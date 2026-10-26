# Mock interviews

Three scripts, ready to run. A mock only counts if you answer out loud, with someone else (or Claude) playing the interviewer, against the clock. Reading these doesn't count.

## How to run one

- **Who plays the interviewer:** a friend, a free peer site such as Pramp, or Claude. To use Claude, start a new chat, paste a script, and say: "Be the interviewer. Ask the opening question and follow-ups one at a time, don't give me answers, and score me with the rubric at the end."
- **Set up:** a 45-minute timer, a whiteboard or a drawing tool such as Excalidraw, and no notes.
- **After it:** fill in the log at the bottom the same day, while you remember where you got stuck.

## Mock 1: Design a rate limiter

**Opening:** "Design a rate limiter for a public API used by millions of clients."

Follow-ups, roughly one per 5 minutes after the high-level design:

1. "Which algorithm, and why not a fixed window?"
2. "Two servers check the same client at the same moment. What goes wrong, and how do you fix it?"
3. "Redis is down. What does your limiter do?"
4. "A whole university is behind one IP. What now?"
5. "We're in three regions. Is the limit global?"

Worked answer: [02-rate-limiter.md](02-rate-limiter.md). Read it after the mock, not before.

## Mock 2: This project, in depth

**Opening:** "Walk me through Support AI. What does it do, and how does a WhatsApp message become an answer?"

Follow-ups:

1. "Twilio sends the same message twice. What happens?"
2. "Why pgvector and not Pinecone?"
3. "Why Logistic Regression and not XGBoost or an LLM, for priority?"
4. "OpenAI takes 20 seconds to answer. What does the customer see?"
5. "Why the 0.8 threshold for angry customers?"
6. "What breaks first if traffic grows 100 times?"
7. "Why did you run it on one EC2 server after building it on EKS?"
8. "Tell me about something that went wrong while you built it." (The EC2 server terminated by mistake, and rebuilt on the same Elastic IP, in the Stage 8 summary.)

Worked answers: the [design doc](../../design-doc.md), the [stage summaries](../../stages/), and [07-support-ai-at-10-million-users.md](07-support-ai-at-10-million-users.md) for follow-up 6.

## Mock 3: Design a chat app

**Opening:** "Design WhatsApp: one-to-one and group messaging for 50 million daily users."

Follow-ups:

1. "Why WebSockets and not polling?"
2. "How does a message reach someone whose app is closed?"
3. "The phone resends a message because it never got the ack. What happens?"
4. "How do you keep messages in order?"
5. "A connection server crashes with 100,000 users on it. What happens to them?"

Worked answer: [04-chat-app.md](04-chat-app.md).

## Rubric

Score each from 1 (missing) to 4 (strong):

| Area | A 4 looks like |
| --- | --- |
| Requirements | Asked questions first, and wrote down the features, scale, and what's out of scope |
| Estimates | Quick, rounded numbers, and said what each one decides |
| API and data model | Concrete endpoints and tables, with the store chosen from how the data is read |
| High-level design | A clear diagram, and one request walked through it end to end |
| Deep dives | Two options for each hard part, the trade-off, and a choice |
| Failures | Said what happens when each part fails, without being asked |
| Communication | Thought out loud, checked in, and took hints well |
| Time | Reached the wrap-up by minute 45 |

## Log

| # | Date | Interviewer | Score (of 32) | Where I got stuck | What to practice next |
| --- | --- | --- | --- | --- | --- |
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
