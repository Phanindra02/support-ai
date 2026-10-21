# Authentication and authorization

Every request to the API answers two questions. Who sent it? And are they allowed to do this? Stage 3 added both: agents log in and get a JWT, and roles decide what each agent may do.

## Authentication vs authorization

**Authentication (authn)** proves who someone is: a password, a token, a signed request, a fingerprint.

**Authorization (authz)** decides what that someone may do: read tickets, update them, manage other agents.

Authentication always comes first, because there's nothing to decide about a stranger. HTTP keeps them apart with two status codes:

- **401 Unauthorized** means "I don't know who you are." The token is missing, expired, or forged. Despite its name, it's about authentication. The response carries `WWW-Authenticate: Bearer` to say how to log in.
- **403 Forbidden** means "I know who you are, and the answer is no." Logging in again won't help.

### Ways to decide what someone may do

- **Role-based access control (RBAC).** Each user has a role, and each role has permissions. It's simple to explain and to check, and it fits most internal tools.
- **Attribute-based access control (ABAC).** Rules use attributes of the user, the resource, and the request: "an agent may close a ticket assigned to them, during their shift." It's more flexible, and harder to test and to reason about.
- **Ownership checks.** "A customer may only read their own tickets." Forgetting one is the classic bug, called IDOR (insecure direct object reference): change the id in the URL and see someone else's data.

## Sessions vs JWTs

Once someone has logged in, every later request has to prove it without sending the password again. There are two main ways.

### Server-side sessions

At login, the server makes a long random session id, stores it in a database or Redis next to the user's id, and sends it back, usually in a cookie. Each request sends the id, and the server looks it up.

- **Logging out is easy and immediate:** delete the row.
- **The id itself means nothing.** Someone who steals the database of sessions gets ids, which can be stored hashed, and the cookie can be `HttpOnly` so page scripts can't read it.
- **Every request needs a lookup,** and every server must reach the same session store. That's why sessions moved from server memory to Redis once apps ran on more than one server.

### JWTs (JSON Web Tokens)

At login, the server signs a small JSON document of claims, such as `{"sub": "7", "exp": 1792550000}`, and sends it back. Each request sends the token, and the server checks the signature and the expiry. It needs no lookup to know who sent the request.

A JWT has three base64url parts separated by dots: a header (which algorithm signed it), the claims, and the signature. The claims are encoded, not encrypted. Anyone holding the token can read them, so they must never carry secrets.

- **No shared store,** so any server with the key can check any token. That suits many services, or services run by different teams.
- **Hard to take back.** A token works until it expires, even after logout or after the account is disabled, unless the server keeps a list of revoked tokens, and then it's doing a lookup again. The usual answer is short-lived access tokens, plus a refresh token that is checked against the database whenever a new access token is issued.
- **Bigger than an id,** and sent on every request.
- **Easy to get wrong.** Libraries once accepted `"alg": "none"`, meaning no signature, or let a token choose an algorithm the server didn't intend. The server must say which algorithms it accepts.

**Symmetric or asymmetric signing.** HS256 uses one secret key to sign and to check, so every checker could also make tokens. RS256 and ES256 sign with a private key and check with a public one, so other services can verify tokens without being able to forge them.

### Where a browser keeps the token

- **`localStorage`:** easy to use, but any script on the page can read it, so a cross-site scripting (XSS) bug leaks every token.
- **An `HttpOnly` cookie:** scripts can't read it, but the browser sends it automatically, including on requests that other sites trigger. That opens cross-site request forgery (CSRF), which `SameSite` cookies and CSRF tokens defend against.
- **The `Authorization` header,** as this API expects, isn't sent automatically, so CSRF doesn't apply. The client has to keep the token somewhere, which brings back the question above.

## OAuth2 and OpenID Connect

**OAuth2 is about delegation.** It lets a user give an app limited access to their account somewhere else without handing over their password. "Let this scheduling app read my Google Calendar." It defines roles: the resource owner (the user), the client (the app), the authorization server (which logs the user in and issues tokens), and the resource server (the API that accepts them).

**OpenID Connect (OIDC)** is a layer on top that adds identity. Besides the access token, the client gets an ID token, a JWT that says who the user is. "Sign in with Google" is OIDC.

### The main flows (grant types)

- **Authorization code, with PKCE.** The app sends the user to the authorization server's login page. After they log in and agree, the browser comes back to the app with a short-lived code, which the app trades for tokens. The user's password only ever goes to the authorization server. PKCE (proof key for code exchange) has the app make a random secret before redirecting and prove it when trading the code, so a stolen code is useless. It's the recommended flow for web, mobile, and single-page apps.
- **Client credentials.** No user at all: one service proves its own identity with its client id and secret, and gets a token. It's for machine-to-machine calls, like a nightly job calling an API.
- **Device code.** For devices with no easy keyboard, like a TV: the device shows a code, and the user types it on their phone.
- **Refresh token.** Trades a long-lived refresh token for a new access token, so a user stays logged in without short-lived tokens forcing them to log in again.
- **Password (resource owner password credentials).** The app collects the user's username and password itself and sends them to the token endpoint. It's only safe when the app and the server belong to the same people. The OAuth 2.0 Security Best Current Practice says not to use it, and OAuth 2.1 drops it.
- **Implicit.** Returned tokens straight in the browser's URL. Replaced by authorization code with PKCE, and also dropped in OAuth 2.1.

## What this project uses, and why

### Agents log in with the password flow, and get a JWT

`POST /auth/token` takes the email and password as an OAuth2 password-flow form, and returns `{"access_token": "...", "token_type": "bearer"}`. The code is in `app/security.py` and `app/routers/auth.py`.

- **Why the password flow.** The API is both the authorization server and the resource server, and only its own agents log in, so there's no third party to delegate to. FastAPI's security tutorial uses this flow, and `/docs` understands it, so the Authorize button logs in for you. If outside apps ever needed access to agents' accounts, the right move would be authorization code with PKCE, probably from an identity provider like Auth0, Cognito, or Okta rather than built by hand.
- **Passwords are hashed with Argon2id** (via pwdlib). Each hash has its own salt, and takes about 30 ms and 64 MB on purpose, so someone who steals the table can only try a few guesses per second for each password.
- **A wrong email takes as long as a wrong password.** When no agent has that email, the code still checks the password against a dummy hash. Otherwise a fast "no" would tell an attacker which emails have accounts. The error message is the same for both cases for the same reason.
- **Tokens are HS256, last 30 minutes, and carry only `sub` (the agent's id) and `exp`.** One service makes and checks them, so a shared secret is enough, and RS256's public key would have no one to give it to. The decode call names `HS256` as the only accepted algorithm, and requires both claims. The id is used rather than the email because an id never changes.
- **The agent is looked up on every request anyway.** The token proves who the agent is, but the API still loads the agent by primary key and checks `is_active`. That costs one indexed lookup per request, and buys the thing JWTs lack: an admin who deactivates an agent locks them out at once, not up to 30 minutes later. This is close to a session, with the token's signature saving a table of session ids.
- **No refresh tokens yet.** After 30 minutes, the agent logs in again. A refresh token would mean storing and rotating tokens, which can come with a real frontend.

### Two roles

`agent` and `admin`, stored on the agent's row. `CurrentAgent` requires a valid login, and `CurrentAdmin` also requires the admin role, or returns a 403. Every ticket endpoint needs a login, and managing agents needs an admin. RBAC with two roles is all this needs, and the check lives in one dependency, so a missing check in a new endpoint is easy to spot in review.

### Machines prove who they are differently

Not every caller is a person with a password:

- **Twilio** signs each webhook request with an HMAC of the URL and form, keyed with the account's auth token. The API recomputes it and compares (see [webhooks](06-webhooks.md)). Tampering with any field, or replaying the body to another URL, breaks the signature.
- **The API calling n8n** sends a shared secret in the `X-Webhook-Secret` header, and n8n's Header Auth checks it. It's simpler than a signature, and relies on HTTPS to keep the secret from being read on the way. It also can't tell a replayed request from a new one.

If the n8n link mattered more, the API could sign each alert with an HMAC and a timestamp, as Stripe and GitHub do for their webhooks.

### Weak spots to know about

- **Password guessing.** Login shares the per-IP limit of 60 requests a minute, and the limit fails open when Redis is down. A stricter limit on `/auth/token`, by IP and by email, that fails closed would be better.
- **No logout.** A client can throw its token away, but the token still works until it expires, unless the agent is deactivated.
- **One secret key.** Anyone with `JWT_SECRET_KEY` can make a token for any agent. It lives in the git-ignored `.env` now, and in AWS Parameter Store from Stage 5. Changing it logs everyone out.

## Interview questions

- What's the difference between a 401 and a 403?
- Sessions or JWTs: which would you pick for a single web app, and which for twenty microservices?
- How do you log a user out, or lock them out, when you use JWTs?
- Why must the server name the algorithms it accepts when it decodes a JWT?
- Why hash passwords with Argon2 or bcrypt instead of SHA-256?
- What does PKCE protect against?
- Which OAuth2 flow would you use for a nightly job calling an API? For a mobile app?
- How is OAuth2 different from OpenID Connect?
- Where should a browser app keep its token, and what attack does each choice expose it to?
