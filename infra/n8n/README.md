# n8n workflows

`urgent-ticket.json` is the workflow that posts urgent tickets to Slack. The API calls it when an agent changes a ticket's priority to urgent (see `app/alerts.py`).

It has two nodes:

1. **Urgent ticket**, a Webhook trigger on `POST /webhook/urgent-ticket`. Its Header Auth only lets a request through if `X-Webhook-Secret` holds the shared secret. Anything else gets a `403`, and the workflow doesn't run.
2. **Post to Slack**, which posts the ticket to `#support-urgent`:

   > :rotating_light: **Urgent ticket #42**: Card charged twice
   > Customer: Jane Doe
   > Status: open. Marked urgent by Ada Lovelace.

The subject and customer name come from customers, so the message escapes `&`, `<`, and `>` in them. Slack reads `<...>` as a link or a mention, so without that, a WhatsApp message whose first line was `<!channel>` would notify everyone in the channel.

The file holds no secrets. n8n keeps credentials in its own encrypted database, and a workflow export only names the credentials it uses.

## Set it up

You need Node.js (n8n runs on it) and a Slack workspace where you can add apps.

1. **Start n8n.** It opens at http://localhost:5678. The first time, it asks you to create an owner account, which only exists on your Mac.

   ```bash
   npx n8n
   ```

   If npx fails with `EACCES` or `EEXIST` under `~/.npm/_cacache`, an earlier `sudo npm` left root-owned files in npm's cache. Give them back to yourself, then run `npx n8n` again:

   ```bash
   sudo chown -R "$(id -u):$(id -g)" ~/.npm
   ```

2. **Make a Slack app.** At https://api.slack.com/apps, choose Create New App, then From scratch, and pick your workspace. Under OAuth & Permissions, add the bot token scope `chat:write`, then Install to Workspace. Copy the Bot User OAuth Token, which starts with `xoxb-`.

3. **Make the channel.** In Slack, create `#support-urgent`, then invite the bot with `/invite @your-app-name`. A bot can only post in channels it's in.

4. **Add the credentials in n8n.** Open the menu, then Credentials, then Add credential:
   - **Header Auth**, named `Support AI webhook secret`. Set Name to `X-Webhook-Secret` and Value to the same value as `N8N_WEBHOOK_SECRET` in `.env`. Make one with `openssl rand -hex 32` if it's still empty.
   - **Slack API**, named `Slack bot`. Paste the `xoxb-` token as the Access Token.

5. **Import the workflow.** Create a workflow, open its `...` menu, choose Import from File, and pick `infra/n8n/urgent-ticket.json`. Open each node and pick the credential you just made, since your credentials have different ids from the ones in the file. Save, then Publish (Activate in older versions), so the production URL starts listening.

6. **Try it.** With n8n running, this should post a test message to `#support-urgent`:

   ```bash
   curl -X POST http://localhost:5678/webhook/urgent-ticket \
     -H "Content-Type: application/json" \
     -H "X-Webhook-Secret: $(grep '^N8N_WEBHOOK_SECRET=' .env | cut -d= -f2-)" \
     -d '{"ticket_id": 1, "subject": "Test alert", "customer_name": "Jane Doe",
          "status": "open", "priority": "urgent",
          "created_at": "2026-10-20T12:00:00Z", "marked_urgent_by": "You"}'
   ```

   Then try the real thing: start the API, log in at `/docs`, and change a ticket's priority to `urgent`.

`/webhook/urgent-ticket` is the production URL, which works while the workflow is active. The editor's "Listen for test event" button uses `/webhook-test/urgent-ticket` instead, and only for one call.

## Change it

Edit the workflow in n8n, then download it from its `...` menu and save it over `urgent-ticket.json`. Before committing, check the diff: keep the `id`, which the command-line import (`n8n import:workflow`) needs, and leave out anything specific to your Mac, like `meta.instanceId`. `tests/test_n8n_workflow.py` checks that the file still matches what the API sends.
