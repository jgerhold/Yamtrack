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
  edition_id
  status_id
  rating
  private_notes
  updated_at
  book { title pages cached_image(path: "url") default_ebook_edition_id }
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


def delete_read(token, read_id, user_book_id):
    """Remove a reading session and its reading journal entries.

    Hardcover keeps the journal entries (progress, finished) of a deleted
    session, so they would still count as reading on their dates.
    """
    query = """
    query ReadJournals($id: Int!) {
      me {
        user_books(where: {id: {_eq: $id}}) {
          reading_journals { id metadata }
        }
      }
    }
    """
    user_books = _me(graphql_request(query, {"id": user_book_id}, token))["user_books"]
    journals = user_books[0]["reading_journals"] if user_books else []
    for journal in journals:
        if (journal.get("metadata") or {}).get("user_book_read_id") == read_id:
            delete_journal(token, journal["id"])

    query = """
    mutation DeleteRead($id: Int!) {
      delete_user_book_read(id: $id) { id error }
    }
    """
    _mutate(token, "delete_user_book_read", query, {"id": read_id})


def delete_journal(token, journal_id):
    """Remove a reading journal entry."""
    query = """
    mutation DeleteJournal($id: Int!) {
      delete_reading_journal(id: $id) { __typename }
    }
    """
    graphql_request(query, {"id": journal_id}, token)


# Owned books ----------------------------------------------------------------
# Hardcover keeps owned books in a list of the user with the slug "owned", one
# row per owned edition. user_books.owned doesn't follow that list.

OWNED_LIST_SLUG = "owned"


def fetch_owned(token):
    """Return the id of the user's Owned list and its rows, or (None, [])."""
    query = """
    query OwnedList($slug: String!) {
      me { lists(where: {slug: {_eq: $slug}}) { id } }
    }
    """
    lists = _me(graphql_request(query, {"slug": OWNED_LIST_SLUG}, token))["lists"]
    if not lists:
        return None, []
    list_id = lists[0]["id"]

    query = """
    query OwnedBooks($list_id: Int!, $limit: Int!, $offset: Int!) {
      list_books(
        where: {list_id: {_eq: $list_id}}
        limit: $limit
        offset: $offset
        order_by: {id: asc}
      ) {
        id
        book_id
        edition_id
        date_added
        book { title cached_image(path: "url") }
      }
    }
    """
    rows = []
    offset = 0
    while True:
        variables = {"list_id": list_id, "limit": PAGE_SIZE, "offset": offset}
        page = (graphql_request(query, variables, token).get("data") or {}).get(
            "list_books",
        ) or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return list_id, rows
        offset += PAGE_SIZE


def fetch_ebook_editions(token, book_ids):
    """Return {book id: id of its default ebook edition} where it has one."""
    query = """
    query EbookEditions($ids: [Int!]) {
      books(where: {id: {_in: $ids}}) { id default_ebook_edition_id }
    }
    """
    ids = sorted({int(book_id) for book_id in book_ids})
    response = graphql_request(query, {"ids": ids}, token)
    return {
        str(book["id"]): book["default_ebook_edition_id"]
        for book in (response.get("data") or {}).get("books") or []
        if book.get("default_ebook_edition_id")
    }


def add_owned(token, list_id, book_id, edition_id):
    """Add a book (edition) to the user's Owned list; return the row id."""
    query = """
    mutation AddOwned($object: ListBookInput!) {
      insert_list_book(object: $object) { id }
    }
    """
    fields = {"list_id": list_id, "book_id": int(book_id)}
    if edition_id:
        fields["edition_id"] = int(edition_id)
    return _mutate(token, "insert_list_book", query, {"object": fields})["id"]


def remove_owned(token, list_book_id):
    """Remove one row (an owned edition) from the Owned list."""
    query = """
    mutation RemoveOwned($id: Int!) {
      delete_list_book(id: $id) { id }
    }
    """
    _mutate(token, "delete_list_book", query, {"id": list_book_id})
