# The interview flow and quick estimates

Each design in this folder follows the same 45-minute flow. Practice it until the order is automatic, so your attention goes to the problem rather than to what comes next.

## The flow, with a time budget

| Minutes | Step | What to do |
| --- | --- | --- |
| 0–5 | **Requirements** | Ask before drawing. List 3 to 5 features (functional), then scale, latency, availability, and consistency (non-functional). Say what's out of scope. Write it all down where the interviewer can see it. |
| 5–10 | **Estimates** | Requests a second (average and peak), storage a year, and memory for a cache. Only the numbers that change the design. Round hard. |
| 10–15 | **API** | The 3 to 5 endpoints or messages, with their main fields and responses. |
| 15–20 | **Data model** | Tables or keys, their main columns, and how each is read. Choose the store from the access pattern. |
| 20–30 | **High-level design** | Draw the boxes and arrows, then walk one request through them end to end. |
| 30–42 | **Deep dives** | The 2 or 3 hard parts. The interviewer often picks them. For each, give two options, their trade-offs, and your choice. |
| 42–45 | **Wrap-up** | What breaks first as load grows, what happens when each part fails, and what you'd build next. |

Habits that help:

- **Think out loud.** The interviewer is grading how you reason, not just the final diagram.
- **Name the trade-off every time you choose.** "I'd use X over Y because Z, and I'd switch when W." The design doc's [trade-offs table](../../design-doc.md#trade-offs) is the model.
- **Start simple, then scale.** One server and one database first, then show where it breaks and fix that part.
- **Check in.** "Does that cover it, or should I go deeper on the storage?" every 10 minutes or so.

## Quick estimates

### Numbers to know by heart

| Fact | Value |
| --- | --- |
| Seconds in a day | 86,400, about 10⁵ |
| 1 million a day | about 12 a second |
| 100 million a day | about 1,200 a second |
| Peak | 2 to 3 times the average, and up to 10 times for bursts |
| 2¹⁰, 2²⁰, 2³⁰, 2⁴⁰ | about a thousand (KB), a million (MB), a billion (GB), a trillion (TB) |
| 99.9% available | about 8.8 hours down a year, or 43 minutes a month |
| 99.99% available | about 53 minutes down a year |

### Rough sizes

| Thing | Size |
| --- | --- |
| An integer, a timestamp, a UUID | 4 or 8 bytes, 8 bytes, 16 bytes |
| A short text post or chat message, with its metadata | about 200 bytes to 1 KB |
| A photo | about 200 KB to 2 MB |
| One embedding from `text-embedding-3-small` | 1,536 numbers × 4 bytes, about 6 KB |

### Rough speeds

| Operation | Time |
| --- | --- |
| Read from memory | about 100 nanoseconds |
| Random read from an SSD | about 0.1 ms |
| Round trip inside one data center, such as a Redis GET | about 0.5 ms |
| Round trip across a continent | about 50 to 150 ms |
| An LLM answer | about 1 to 6 seconds |

And this project's own numbers, from [`results.md`](../../results.md) and the design doc:

- One vCPU served about 160 API requests a second in the load test.
- An indexed first page of tickets took 0.02 ms, while `OFFSET` past 50,000 rows took 7.59 ms.
- With redis-py's default timeouts, a call to a Redis that was down took 2.7 seconds. That's why the cache gives up after 0.5 seconds.

### How to do the arithmetic

1. Start from users: daily active users × actions each a day = actions a day.
2. Divide by 10⁵ for a second, then multiply by 2 to 10 for the peak.
3. Storage: actions a day × bytes each × 365 × years. Add replication (often 3 copies) only if it changes the choice.
4. Cache: the hot 20% of a day's reads usually fits in one or a few Redis nodes. Check that it does.

Worked example, from the [design doc](../../design-doc.md#scale-estimates): 5,000 WhatsApp messages a day ÷ 86,400 = 0.06 a second. Half of them in 4 busy hours is 0.17 a second, and 10 times that for a burst is about 2 a second. The answer: one small server is plenty, and the real limit is how long OpenAI takes, not how many requests arrive.
