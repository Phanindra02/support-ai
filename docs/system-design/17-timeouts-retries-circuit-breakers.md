# Timeouts, retries with backoff, and circuit breakers

Once one service calls another over a network, the call can fail in ways a function call can't: the other side is down, restarting, slow, or overloaded. These three patterns decide what the caller does about it.

## Timeouts

Without a timeout, a call to a stuck service waits forever, and so does whatever made it. Every outbound call in this project has one:

| Call | Timeout | Why that number |
|---|---|---|
| Worker to the model service | 1s to connect, 5s to answer | It answers in milliseconds, and a connection inside the cluster takes less. |
| API to OpenAI for a bot answer | 6s per call, no retries | Twilio waits 15s for the webhook's reply, and a person is a better fallback than a late answer. |
| API and worker to n8n | 5s | A late Slack alert is fine, a stuck worker isn't. |
| API to the Redis cache | 0.5s, no retries | The cache is optional, so a slow Redis is skipped. redis-py's default retries made each call take 2.7s while Redis was down. |
| API and worker to S3 | 5s to connect, 30s to read | A document can be large. |
| Startup to Parameter Store | 5s to connect, 10s to read, 3 attempts | The app can't start without its settings, so it should fail in seconds, not boto3's default of minutes. |

Connect and read timeouts are split because they mean different things. A slow connect means the host isn't there, so there's no point waiting long. A slow read can be real work.

## Retries with backoff

Many failures pass by themselves: a pod restarting during a deploy, a 503 while a load balancer notices a new pod. Asking again fixes those. Two rules keep retries from doing harm:

1. **Only retry what might change.** A refused connection, a timeout, or a 502, 503, or 504 might pass. A 422 means the request is wrong and always will be, so it raises at once.
2. **Only retry what's safe to repeat.** If a timed-out request actually reached the server, a retry runs it twice. A prediction changes nothing, so that's fine here. A call that charges a card would need an idempotency key, the same idea as the Twilio message ID that stops a webhook from being handled twice (`06-webhooks.md`).

**Backoff** means waiting longer after each failure, so a struggling service gets room to recover. **Jitter** means making each wait random. Without it, every worker that failed at the same moment retries at the same moment too, and the load arrives in spikes. With "full jitter", each wait is random between 0 and a cap that doubles each time.

## How this project does it

There are two layers, at two time scales:

- **Inside the job** (`ask_model` in `worker/jobs.py`): up to 3 attempts, waiting a random 0 to 0.5 seconds, then 0 to 1 second. That covers a model service pod restarting, without failing the job.
- **Across jobs** (RQ's `TRIAGE_RETRY` in `app/queue.py`): if the job still fails, RQ runs it again after 5 seconds, 30 seconds, then 2 minutes. That covers a longer outage. After that, the job waits in RQ's failed job registry, and the ticket keeps its medium priority until an agent sets one.

Retries multiply, so the layers have to stay small. Here it's at most 3 attempts per job and 4 runs of the job, so 12 calls for one ticket in the worst case, spread over about 3 minutes. If the API also retried, and the model service retried something of its own, a single failure deep inside would turn into dozens of calls. That's a retry storm.

## Circuit breakers

A circuit breaker sits in front of calls to one service and counts failures. After too many in a row it "opens", and calls fail at once without being sent. After a pause it lets one trial call through ("half-open"): if that works it closes again, and if not it stays open.

It helps in two ways. The caller stops wasting time, threads, and connections waiting on a service that's down. And the failing service stops getting traffic while it tries to recover.

This project doesn't have one, on purpose. The only caller of the model service is the worker, which works in the background, one job at a time per pod. Nothing waits on it, and RQ's growing waits already back off much as an open breaker would. A breaker earns its place on a synchronous path. If the API ever asked the model service while a customer waited, it should fail fast once the service looked down, and answer with medium priority straight away rather than make every request wait for a timeout first. Libraries such as `pybreaker` do this.

## In an interview

- Every network call needs a timeout. The number comes from the caller's own deadline.
- Retry only failures that might pass, only requests safe to repeat, with backoff and jitter, and in one layer if possible.
- A circuit breaker protects the caller and gives the callee room. It matters most where a person is waiting.
