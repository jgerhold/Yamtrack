"""GraphQL calls for a user's Hardcover library.

Every call takes the user's own token (tokens.require_user_token): the sync
never runs on the shared token.
"""

from app.providers.hardcover import graphql_request

PAGE_SIZE = 100
MERGED_PAGE_SIZE = 500

USER_BOOK_FIELDS = """
  id
  book_id
  status_id
  rating
  private_notes
  updated_at
  book { title pages cached_image(path: "url") }
  user_book_reads(order_by: [{started_at: asc_nulls_last}, {id: asc}]) {
    id
    started_at
    finished_at
    progress_pages
  }
"""


class HardcoverSyncError(Exception):
    """Raised when Hardcover refuses a change."""


def _me(response):
    """Return the current user from Hardcover's one-element `me` list."""
    me = (response.get("data") or {}).get("me")
    if isinstance(me, list):
        me = me[0] if me else None
    if not me:
        msg = "Hardcover returned no user for this token"
        raise HardcoverSyncError(msg)
    return me


def fetch_library(token):
    """Return every entry of the user's Hardcover library."""
    query = f"""
    query Library($limit: Int!, $offset: Int!) {{
      me {{
        user_books(limit: $limit, offset: $offset, order_by: {{id: asc}}) {{
          {USER_BOOK_FIELDS}
        }}
      }}
    }}
    """
    user_books = []
    offset = 0
    while True:
        page = _me(
            graphql_request(query, {"limit": PAGE_SIZE, "offset": offset}, token),
        )["user_books"]
        user_books.extend(page)
        if len(page) < PAGE_SIZE:
            return user_books
        offset += PAGE_SIZE


def fetch_user_book(token, book_ids):
    """Return the user's library entry for one of the Hardcover books, or None.

    book_ids are editions of the same book in order of preference.
    """
    book_ids = [int(book_id) for book_id in book_ids]
    query = f"""
    query LibraryEntry($book_ids: [Int!]) {{
      me {{
        user_books(where: {{book_id: {{_in: $book_ids}}}}) {{
          {USER_BOOK_FIELDS}
        }}
      }}
    }}
    """
    entries = _me(graphql_request(query, {"book_ids": book_ids}, token))["user_books"]
    return min(
        entries,
        key=lambda entry: book_ids.index(entry["book_id"]),
        default=None,
    )


def fetch_merged_books(token, book_ids):
    """Return {book id: id it was merged into} for books Hardcover merged.

    Hardcover marks a merged book as a duplicate of another (canonical) book
    and refuses the duplicate in libraries.
    """
    query = """
    query MergedBooks($ids: [Int!]) {
      books(where: {
        id: {_in: $ids}, state: {_eq: "duplicate"}, canonical_id: {_is_null: false}
      }) {
        id
        canonical_id
      }
    }
    """
    ids = sorted({int(book_id) for book_id in book_ids})
    merged = {}
    for start in range(0, len(ids), MERGED_PAGE_SIZE):
        response = graphql_request(
            query,
            {"ids": ids[start : start + MERGED_PAGE_SIZE]},
            token,
        )
        for book in (response.get("data") or {}).get("books") or []:
            merged[str(book["id"])] = str(book["canonical_id"])
    return merged


def _mutate(token, name, query, variables):
    """Run a mutation and return its result, raising on Hardcover's error field."""
    result = (graphql_request(query, variables, token).get("data") or {}).get(name)
    if not result:
        msg = f"{name}: Hardcover returned no result"
        raise HardcoverSyncError(msg)
    if result.get("error"):
        msg = f"{name}: {result['error']}"
        raise HardcoverSyncError(msg)
    return result


def insert_user_book(token, fields):
    """Add a book to the library; return the new user_books id."""
    query = """
    mutation AddBook($object: UserBookCreateInput!) {
      insert_user_book(object: $object) { id error }
    }
    """
    return _mutate(token, "insert_user_book", query, {"object": fields})["id"]


def update_user_book(token, user_book_id, fields):
    """Change a library entry."""
    query = """
    mutation UpdateBook($id: Int!, $object: UserBookUpdateInput!) {
      update_user_book(id: $id, object: $object) { id error }
    }
    """
    _mutate(
        token,
        "update_user_book",
        query,
        {"id": user_book_id, "object": fields},
    )


def delete_user_book(token, user_book_id):
    """Remove a book from the library."""
    query = """
    mutation DeleteBook($id: Int!) {
      delete_user_book(id: $id) { id }
    }
    """
    _mutate(token, "delete_user_book", query, {"id": user_book_id})


def insert_read(token, user_book_id, read):
    """Add a reading session to a library entry; return its id."""
    query = """
    mutation AddRead($user_book_id: Int!, $read: DatesReadInput!) {
      insert_user_book_read(user_book_id: $user_book_id, user_book_read: $read) {
        id error
      }
    }
    """
    return _mutate(
        token,
        "insert_user_book_read",
        query,
        {"user_book_id": user_book_id, "read": read},
    )["id"]


def update_read(token, read_id, read):
    """Change a reading session."""
    query = """
    mutation UpdateRead($id: Int!, $read: DatesReadInput!) {
      update_user_book_read(id: $id, object: $read) { id error }
    }
    """
    _mutate(token, "update_user_book_read", query, {"id": read_id, "read": read})


def delete_read(token, read_id):
    """Remove a reading session."""
    query = """
    mutation DeleteRead($id: Int!) {
      delete_user_book_read(id: $id) { id error }
    }
    """
    _mutate(token, "delete_user_book_read", query, {"id": read_id})
