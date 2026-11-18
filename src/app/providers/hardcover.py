import json
import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext as _

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services
from hardcover_sync import tokens

logger = logging.getLogger(__name__)

base_url = "https://api.hardcover.app/v1/graphql"
MAX_SEARCH_QUERY_LENGTH = 50

# Search syntax keyword -> Hardcover search document field
SEARCH_FILTER_FIELDS = {
    "author": "author_names",
    "series": "series_names",
}
SEARCH_FILTER_PATTERN = re.compile(
    r'(?<!\S)(author|series):(?:"([^"]*)"|(\S+))',
    re.IGNORECASE,
)

# Contribution roles that count as the book's authors (None is the default role)
AUTHOR_CONTRIBUTIONS = {None, "", "Author"}


def cap_search_query(query):
    """Limit long book queries before sending them to Hardcover search."""
    query = str(query or "")

    if len(query) <= MAX_SEARCH_QUERY_LENGTH:
        return query

    capped_query = query[:MAX_SEARCH_QUERY_LENGTH]
    if query[MAX_SEARCH_QUERY_LENGTH].isspace():
        return capped_query.rstrip()

    word_boundary = capped_query.rfind(" ")
    if word_boundary == -1:
        return capped_query

    return capped_query[:word_boundary].rstrip()


def handle_error(error):
    """Handle Hardcover API errors."""
    error_resp = error.response
    status_code = error_resp.status_code

    try:
        error_json = error_resp.json()
    except requests.exceptions.JSONDecodeError as json_error:
        logger.exception("Failed to decode JSON response")
        raise services.ProviderAPIError(Sources.HARDCOVER.value, error) from json_error

    if status_code == requests.codes.unauthorized:
        details = error_json.get("error_description") or error_json.get("error")
        raise services.ProviderAPIError(Sources.HARDCOVER.value, error, details)

    raise services.ProviderAPIError(Sources.HARDCOVER.value, error)


def graphql_request(query, variables, token=None):
    """Make a GraphQL request to Hardcover and return the response.

    Without an explicit token, the signed-in user's own token is used when they
    saved one, otherwise the shared HARDCOVER_API token (see hardcover_sync.tokens).
    """
    user_token = None if token else tokens.current_user_token()
    authorization = token or user_token or settings.HARDCOVER_API
    try:
        response = services.api_request(
            Sources.HARDCOVER.value,
            "POST",
            base_url,
            params={"query": query, "variables": variables},
            headers={"Authorization": authorization},
        )
    except requests.exceptions.HTTPError as error:
        if user_token and error.response.status_code == requests.codes.unauthorized:
            raise services.ProviderAPIError(
                Sources.HARDCOVER.value,
                error,
                _(
                    "Hardcover rejected your personal API token. Update it under "
                    "Settings > Integrations, or remove it to use the shared token.",
                ),
            ) from error
        handle_error(error)

    # GraphQL errors are returned with a 200 status code
    errors = response.get("errors")
    if errors:
        mock_response = type(
            "obj",
            (object,),
            {
                "status_code": requests.codes.ok,
                "text": json.dumps(errors),
            },
        )()
        mock_error = requests.exceptions.HTTPError(response=mock_response)
        raise services.ProviderAPIError(
            Sources.HARDCOVER.value,
            mock_error,
            errors[0].get("message"),
        )

    return response


def get_username(token):
    """Return the Hardcover username a token belongs to.

    Raises ProviderAPIError when Hardcover rejects the token.
    """
    response = graphql_request("query CurrentUser { me { username } }", {}, token)
    me = (response.get("data") or {}).get("me")
    # Hardcover returns the current user as a one-element list
    if isinstance(me, list):
        me = me[0] if me else {}
    return (me or {}).get("username")


def parse_search_filters(query):
    """Split a search query into free text and field filters.

    Supports ``author:"Blake Crouch"`` and ``series:"Wayward Pines"``. A quoted
    value must match the whole name (case-insensitive), an unquoted word matches
    any name containing it, e.g. ``author:crouch``. Returns a tuple of
    (text, filters), where filters is a list of (search field, value, exact)
    tuples in the order they appeared.
    """
    filters = []

    def collect(match):
        exact = match.group(2) is not None
        value = match.group(2) if exact else match.group(3)
        # Backticks delimit values in Typesense filters, so they can't be escaped
        value = value.strip().strip('"').replace("`", "").strip()
        if value:
            field = SEARCH_FILTER_FIELDS[match.group(1).lower()]
            filters.append((field, value, exact))
        return " "

    text = SEARCH_FILTER_PATTERN.sub(collect, str(query or ""))
    return " ".join(text.split()), filters


def build_filter_by(filters):
    """Build a Typesense filter_by expression from parsed search filters.

    ``:=`` matches the whole value, ``:`` matches values containing the words.
    """
    return " && ".join(
        f"{field}:{'=' if exact else ''}[`{value}`]" for field, value, exact in filters
    )


def search(query, page):
    """Search for books on Hardcover.

    Besides plain text, the query can filter by author or series, for example
    ``author:"Blake Crouch"`` or ``dark series:"Wayward Pines"``.
    """
    text, filters = parse_search_filters(query)
    if not text and not filters:
        # Only empty filters like author:"" — don't send "*" and list every book
        return helpers.format_search_response(page, settings.PER_PAGE, 0, [])

    query = cap_search_query(text)
    filter_by = build_filter_by(filters) or None

    cache_query = f"{query}_{filter_by}" if filter_by else query
    cache_key = (
        f"search_{Sources.HARDCOVER.value}_{MediaTypes.BOOK.value}_{cache_query}_{page}"
    )
    data = cache.get(cache_key)

    if data is None:
        search_query = """
        query SearchBooks(
          $query: String!,
          $per_page: Int!,
          $page: Int!,
          $filter_by: String,
          $sort: String,
        ) {
          search(
            query: $query,
            query_type: "Book",
            per_page: $per_page,
            page: $page,
            filter_by: $filter_by,
            sort: $sort,
          ) {
            results
          }
        }
        """

        variables = {
            # Typesense uses "*" to match everything, so filters work without text
            "query": query or "*",
            "per_page": settings.PER_PAGE,
            "page": page,
            "filter_by": filter_by,
            # Without text there is no relevance, so show the most read books first
            "sort": "users_count:desc" if filter_by and not query else None,
        }

        response = graphql_request(search_query, variables)

        # Hardcover returns null results when a filter can't be applied
        search_results = response["data"]["search"]["results"] or {}
        hits = search_results.get("hits", [])
        results = [
            {
                "media_id": hit["document"]["id"],
                "source": Sources.HARDCOVER.value,
                "media_type": MediaTypes.BOOK.value,
                "title": hit["document"]["title"],
                "image": get_image_url(hit["document"]),
            }
            for hit in hits
        ]
        total_results = search_results.get("found", 0)

        data = helpers.format_search_response(
            page,
            settings.PER_PAGE,
            total_results,
            results,
        )

        cache.set(cache_key, data)

    return data


def book(media_id):
    """Get metadata for a book from Hardcover."""
    cache_key = f"{Sources.HARDCOVER.value}_{MediaTypes.BOOK.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        book_query = """
        query GetBookDetails($book_id: Int!) {
          books_by_pk(id: $book_id) {
            id
            title
            cached_image(path: "url")
            description
            cached_tags(path: "Genre")
            rating
            ratings_count
            pages
            release_date
            slug
            contributions {
              contribution
              author {
                name
              }
            }
            book_series(order_by: [{featured: desc}, {position: asc}]) {
              position
              series {
                name
              }
            }
            default_cover_edition {
              edition_format
              isbn_13
              isbn_10
              release_date
              publisher {
                name
              }
            }
          }
        }
        """

        variables = {
            "book_id": int(media_id),
        }

        response = graphql_request(book_query, variables)

        book_data = response["data"]["books_by_pk"]

        if not book_data:
            services.raise_not_found_error(
                Sources.HARDCOVER.value,
                media_id,
                "book",
            )

        edition_details = get_edition_details(book_data.get("default_cover_edition"))
        authors = get_authors(book_data.get("contributions"))
        series = get_series(book_data.get("book_series"))

        data = {
            "media_id": book_data["id"],
            "source": Sources.HARDCOVER.value,
            "source_url": f"https://hardcover.app/books/{book_data['slug']}",
            "media_type": MediaTypes.BOOK.value,
            "title": book_data["title"],
            "max_progress": book_data.get("pages"),
            "image": book_data.get("cached_image") or settings.IMG_NONE,
            "synopsis": book_data.get("description") or "No synopsis available.",
            "genres": get_tags(book_data.get("cached_tags")),
            "score": get_ratings(book_data.get("rating")),
            "score_count": book_data.get("ratings_count", 0),
            "details": {
                "format": edition_details.get("format"),
                "number_of_pages": book_data.get("pages"),
                "publish_date": edition_details.get("release_date")
                or book_data.get("release_date"),
                "author": authors or None,
                "series": [label for label, _name in series] or None,
                "publisher": edition_details.get("publisher"),
                "isbn": edition_details.get("isbn"),
            },
            # Detail values that link to a search, keyed by detail then value
            "detail_links": {
                "author": {
                    name: helpers.search_filter_query("author", name)
                    for name in authors
                },
                "series": {
                    label: helpers.search_filter_query("series", name)
                    for label, name in series
                },
            },
        }

        cache.set(cache_key, data)

    return data


def get_authors(contributions):
    """Get the unique author names from the book's contributions.

    Other roles like illustrators or translators are skipped, unless there are
    no authors at all.
    """
    if not contributions:
        return []

    names = [
        contribution["author"]["name"]
        for contribution in contributions
        if contribution.get("contribution") in AUTHOR_CONTRIBUTIONS
        and contribution.get("author")
    ]
    if not names:
        names = [
            contribution["author"]["name"]
            for contribution in contributions[:1]
            if contribution.get("author")
        ]

    return list(dict.fromkeys(name for name in names if name))


def get_series(book_series):
    """Get the series a book belongs to as (label, series name) tuples.

    The label adds the book's position, e.g. "Wayward Pines (#1)".
    """
    if not book_series:
        return []

    series = []
    seen = set()
    for entry in book_series:
        name = (entry.get("series") or {}).get("name")
        if not name or name in seen:
            continue
        seen.add(name)

        position = entry.get("position")
        # 1.0 -> "1", 1.5 -> "1.5"
        label = name if position is None else f"{name} (#{float(position):g})"
        series.append((label, name))

    return series


def get_tags(tags_data):
    """Get processed tags/genres from API data."""
    if not tags_data:
        return None
    return [tag["tag"] for tag in tags_data]


def get_ratings(rating_data):
    """Get processed rating from API data."""
    if not rating_data:
        return None
    return round(float(rating_data) * 2, 1)


def get_edition_details(edition_data):
    """Get processed edition details from API data."""
    if not edition_data:
        return {}

    isbns = []
    if edition_data.get("isbn_10"):
        isbns.append(edition_data["isbn_10"])
    if edition_data.get("isbn_13"):
        isbns.append(edition_data["isbn_13"])

    publisher_name = None
    if edition_data.get("publisher"):
        publisher_name = edition_data["publisher"].get("name")

    return {
        "format": edition_data.get("edition_format") or "Unknown",
        "publisher": publisher_name,
        "isbn": isbns or None,
        "release_date": edition_data.get("release_date"),
    }


def get_image_url(response):
    """Get the cover image URL for a book."""
    if response.get("image") and response["image"].get("url"):
        return response["image"]["url"]
    return settings.IMG_NONE
