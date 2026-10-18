# REST API design

The conventions behind the ticket API's URLs, methods, status codes, and pagination, plus idempotency and versioning, which matter more as the project grows.

## Resources are nouns, and methods are the verbs

A URL names a thing. The HTTP method says what to do with it.

| Method and path | Meaning |
| --- | --- |
| `GET /tickets` | List tickets |
| `POST /tickets` | Create a ticket |
| `GET /tickets/{id}` | Get one ticket |
| `PATCH /tickets/{id}` | Change some fields of a ticket |

Conventions this follows:
- **Plural nouns** (`/tickets`, not `/ticket` or `/getTickets`). The collection and its items share one name.
- **Nesting shows ownership, one level deep.** Stage 2's messages belong to a ticket, so they'll live at `/tickets/{id}/messages`.
- **Filters go in the query string,** like `GET /tickets?status=open&priority=urgent`, not in the path.
- **Some actions aren't resources, and that's fine.** `POST /ask` (Stage 6) and `POST /webhooks/twilio` (Stage 3) are actions. Forcing them into nouns would only make them harder to understand.

There's no `DELETE /tickets/{id}`. Support tickets are a record of what happened, so they get closed, not deleted.

**PUT vs PATCH:** PUT replaces the whole resource with the body sent. PATCH changes only the fields sent. Agents only ever change status or priority, so the API uses PATCH, and `TicketUpdate` rejects every other field.

## Status codes say what happened

| Code | Meaning | In this API |
| --- | --- | --- |
| 200 OK | It worked, and here's the result | `GET` and `PATCH` |
| 201 Created | A new resource exists | `POST /tickets` |
| 204 No Content | It worked, with nothing to return | Not used yet (a typical `DELETE` reply) |
| 400 Bad Request | The request couldn't be read at all | Rare: FastAPI's parser gives up, as on JSON nested a million levels deep. Invalid JSON gets a 422 |
| 401 Unauthorized | Who are you? Log in first | Stage 3, a missing or bad JWT |
| 403 Forbidden | I know who you are, and you can't do this | Stage 3, an agent trying an admin action |
| 404 Not Found | No such resource | A ticket id that doesn't exist |
| 409 Conflict | It clashes with the current state | Possible later, like a duplicate agent email |
| 422 Unprocessable Content | Readable, but the values break the rules | Every Pydantic validation failure |
| 429 Too Many Requests | Slow down | Stage 2's rate limit |
| 500 Internal Server Error | The server broke | Never on purpose. No input should cause one |

**4xx means the client should change the request. 5xx means the server failed, and a retry might work.** That's why the API caps ids at `MAX_INT` and handles unusual JSON: a client's mistake should come back as a 422 that explains it, never as a 500 that looks like the server's fault.

Errors share one shape, so clients can handle them the same way. A 404 returns `{"detail": "Ticket not found"}`. A 422 returns FastAPI's standard list, `{"detail": [{"type", "loc", "msg", "input"}]}`, where `loc` points to the bad field.

A possible improvement: a 201 usually includes a `Location: /tickets/42` header that points at the new resource. The API returns the full ticket in the body instead, which is enough for its clients today.

## Pagination keeps responses small

`GET /tickets` never returns everything. It takes `skip` (how many to pass over, default 0) and `limit` (how many to return, default 20, at most 100). This is **offset pagination**.

Results are sorted newest first, by `created_at` and then `id`. The `id` matters: without a unique tie-breaker, two tickets created in the same second could swap places between requests, and a client could see one twice and miss the other.

Offset pagination is simple and lets a client jump to any page, but it has two weaknesses:
1. **Deep pages are slow.** `OFFSET 50000` makes the database read 50,000 rows just to throw them away. Stage 2 seeds 100,000 tickets, which can show this in `EXPLAIN ANALYZE`.
2. **Pages shift under the client.** If a new ticket arrives while someone is on page 2, every ticket moves down one place, and page 3 repeats a ticket.

**Cursor (keyset) pagination** fixes both. The client sends the last item it saw, and the query asks for what comes after it:

```sql
SELECT * FROM tickets
WHERE (created_at, id) < (:last_created_at, :last_id)
ORDER BY created_at DESC, id DESC
LIMIT 20;
```

An index on `(created_at, id)` takes the database straight to the right spot, so every page is equally fast, and new tickets can't shift later pages. The cost is that a client can't jump to page 37, only to the next page. Offset is fine for an agent dashboard. A feed or a huge export would use cursors.

`limit` has a maximum so that no client can ask for a million rows in one request. `skip` has a maximum (`MAX_INT`) because no ticket id can be bigger, so a bigger skip could never return anything, and numbers past the database's integer range would crash the query.

## Idempotency makes retries safe

A request is **idempotent** if sending it twice has the same effect as sending it once. This matters because networks fail in the middle: a client that times out can't tell whether the server got the request, so it retries.

- `GET`, `PUT`, and `DELETE` are idempotent by definition.
- `POST` is not. Retrying `POST /tickets` after a timeout can create two tickets.
- `PATCH` can go either way. Ours is idempotent: `{"status": "closed"}` sets a value rather than toggling it, and closing a ticket that's already closed keeps the original `closed_at`. A PATCH like "add 1 to the reopen count" would not be idempotent.

The standard fix for POST is an **idempotency key**. The client sends a unique `Idempotency-Key` header, and the server stores the key with its response. A retry with the same key gets the stored response instead of creating a second ticket. Stripe's API works this way. Stage 3 uses the same idea for Twilio: the webhook skips any message ID it has already stored, so a repeat delivery does nothing.

## Versioning lets the API change without breaking clients

A **breaking change** forces clients to change: renaming or removing a field, adding a required field, or changing what a field means. Adding an optional field or a new endpoint isn't breaking.

Ways to version:
- **In the URL:** `/v1/tickets`. It's visible, easy to route and cache, and the most common choice.
- **In a header:** `Accept: application/vnd.supportai.v2+json`. URLs stay clean, but it's harder to try in a browser.
- **In the query string:** `/tickets?version=2`. It's simple, but easy to forget.

The API is unversioned today, because its only clients are in this repo and change with it. It should move to `/v1` before an outside client depends on it, such as a mobile app or another team. In FastAPI that's one line, `APIRouter(prefix="/v1")`. Then `/v1` stays frozen while `/v2` changes, until no one uses `/v1` anymore.

## Interview questions

- Why `PATCH /tickets/{id}` instead of `POST /tickets/{id}/close`? When is an action endpoint the better choice?
- What's the difference between 401 and 403? Between 400 and 422?
- Why sort by `created_at` and `id` instead of `created_at` alone?
- When would you switch from offset to cursor pagination?
- Is your PATCH idempotent? How would you make `POST /tickets` safe to retry?
- How would you rename a field without breaking existing clients?
