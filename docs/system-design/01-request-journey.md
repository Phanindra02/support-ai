# How a request reaches the API

What happens between `curl https://support.example.com/tickets` and the JSON coming back, and how it changes as this project moves from a laptop to AWS.

## The whole trip

```text
client ──DNS──> IP address
client ──TCP + TLS──> load balancer or reverse proxy ──HTTP──> uvicorn ──> FastAPI ──> SQLAlchemy ──> database
client <──────────────────────────── JSON response <───────────────────────────────────┘
```

Today only the right half exists: `http://127.0.0.1:8000` has no DNS lookup, no TLS, and no proxy.

## 1. DNS turns a name into an IP address

A client asks a resolver, "what's the IP for support.example.com?" The answer comes from the first cache that has it: the browser, the operating system (which also reads `/etc/hosts`, where `localhost` lives), then a recursive resolver (your ISP's or 1.1.1.1). On a full miss, the resolver walks the tree: a root server points to the `.com` servers, which point to the domain's own nameservers, which give the answer.

Common records: **A** (name to IPv4 address), **AAAA** (IPv6), and **CNAME** (one name is an alias for another). Every answer has a **TTL**, the number of seconds resolvers may cache it.

In this project:
- Stage 5: an A record points the domain at the EC2 instance's fixed (Elastic) IP.
- Stage 8: the domain points at the AWS load balancer through a CNAME or alias record, because a load balancer's IPs change.
- Moving the domain to EKS and back to EC2 depends on the TTL. A low TTL (like 60 seconds) means clients switch quickly, at the cost of more DNS lookups. Lower it a day before a planned move.

## 2. TCP and TLS open a secure connection

**TCP** opens a reliable connection with a three-step handshake (SYN, SYN-ACK, ACK), which costs one round trip. **TLS 1.3** adds one more round trip. It agrees on encryption keys, and the server sends a certificate that proves it owns the domain. Both costs are paid once per connection, so clients reuse connections (HTTP keep-alive) instead of opening one per request.

In this project, TLS ends at the proxy, not at uvicorn ("TLS termination"):
- Stage 5: Caddy gets a free Let's Encrypt certificate and renews it automatically.
- Stage 8: the AWS load balancer uses a free ACM certificate.

Traffic from the proxy to the API runs on a private network, so the API itself speaks plain HTTP.

## 3. HTTP carries the request and response

A request is a method and path, headers, and an optional body. A response is a status code, headers, and a body.

```http
POST /tickets HTTP/1.1
Host: support.example.com
Content-Type: application/json

{"customer_name": "Jane Doe", "phone": "+16145550123", "subject": "Order late", "message": "Where is my order?"}
```

```http
HTTP/1.1 201 Created
Content-Type: application/json

{"id": 1, "status": "open", "priority": "medium", ...}
```

HTTP is stateless: each request carries everything the server needs, like the JWT in Stage 3. That's what lets any copy of the API answer any request.

## 4. Inside the server

1. **uvicorn**, the ASGI server, reads the HTTP bytes off the socket and hands FastAPI a structured request.
2. **FastAPI** matches the method and path to an endpoint (no match gives 404 or 405).
3. **Dependencies** run: `get_db` opens a database session for this request only.
4. **Pydantic** checks the path, query, and body. Bad input stops here with a 422, before any endpoint code runs.
5. **The endpoint** runs SQL through SQLAlchemy. A missing ticket raises a 404.
6. **The response model** (`TicketRead`) shapes the result into JSON, and `get_db` closes the session.

## 5. Load balancers and reverse proxies

A **reverse proxy** sits in front of servers and forwards requests to them. A **load balancer** is a reverse proxy that spreads requests across several copies of a server. Both give one public entry point, handle TLS, and hide the servers behind them.

- **Layer 4** load balancers forward TCP connections without reading them. They're fast but can't route by path.
- **Layer 7** load balancers read the HTTP request, so they can route `/api` and `/webhooks` differently and add headers.
- **Common strategies:** round robin (take turns), least connections (pick the least busy), and hashing on a key such as the client IP (the same client keeps going to the same server).
- **Health checks** take a failing server out of rotation. Stage 8 adds `/health` endpoints for this.

In this project, Caddy (Stage 5) is a reverse proxy in front of one server, and the AWS Application Load Balancer (Stage 8) is a layer 7 load balancer in front of several API pods.

Two consequences for the code:
- **The API must stay stateless.** Anything that must survive between requests goes in Postgres or Redis, never in a Python variable, because the next request may reach a different pod.
- **The client's IP arrives in a header.** Behind a proxy, every request seems to come from the proxy. The real client IP comes in `X-Forwarded-For`, so Stage 2's per-IP rate limiting must read it, and trust it only when the request came through our own proxy. Otherwise anyone could fake the header.

## Where the time goes

On a typical first request: DNS takes 0 ms when cached and about 20 to 100 ms when not, TCP and TLS take two round trips (about 20 to 200 ms depending on distance), and the API itself takes a few milliseconds for a simple query. Reused connections and DNS caching remove most of the network cost, which is why repeat requests are much faster than the first.

## Interview questions

- What happens when you type a URL into a browser and press Enter?
- Why would you lower a DNS TTL before a migration?
- Where should TLS end, and why is plain HTTP behind the proxy acceptable?
- What's the difference between a layer 4 and a layer 7 load balancer?
- How does the API find the real client IP behind a load balancer, and why can't it trust `X-Forwarded-For` blindly?
