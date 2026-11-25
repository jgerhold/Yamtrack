"""GraphQL calls for a user's Hardcover library.

Every call takes the user's own token (tokens.require_user_token): the sync
never runs on the shared token.
"""

from app.providers.hardcover import graphql_request

PAGE_SIZE = 100

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


def fetch_user_book(token, book_id):
    """Return the user's library entry for a Hardcover book, or None."""
    query = f"""
    query LibraryEntry($book_id: Int!) {{
      me {{
        user_books(where: {{book_id: {{_eq: $book_id}}}}, limit: 1) {{
          {USER_BOOK_FIELDS}
        }}
      }}
    }}
    """
    entries = _me(graphql_request(query, {"book_id": int(book_id)}, token))[
        "user_books"
    ]
    return entries[0] if entries else None


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
