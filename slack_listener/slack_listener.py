# slack_listener.py

import re
import os
import sys
import json
import signal
import logging
import requests
import tempfile
import threading
from slack_bolt import App
from typing import Dict, Any
from slack_sdk.errors import SlackApiError
from datetime import datetime, timezone
from slack_bolt.adapter.socket_mode import SocketModeHandler
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
# ── 1) Config ─────────────────────────────────────────────────────────
class Settings(BaseSettings):
    slack_bot_token:       str
    slack_signing_secret:  str
    slack_app_token:       str
    override_post_channel: str   = "" #"C08R7R0D7PH"
    default_backend_url:   str   = "https://backend-slack-ai-bot.platform-sre.central.di11.lan.didevops.com/fetch-logs"
    tracked_alertnames:    str   = "PodCrashLooping,PodInErrorState,PodPendingTooLong"
    log_level:             str   = "DEBUG"
    retry_attempts:        int   = 2
    retry_backoff_base:    float = 1.0
    retry_backoff_max:     float = 10.0

    model_config = SettingsConfigDict(env_file="/tmp/.slackaibotenv")

    @property
    def tracked_list(self):
        return [a.strip() for a in self.tracked_alertnames.split(",") if a.strip()]

settings = Settings()

# ── 2) Logging ────────────────────────────────────────────────────────
logger = logging.getLogger("alert-listener")
handler_log = logging.StreamHandler()
handler_log.setFormatter(logging.Formatter(
    '{"@timestamp":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}'
))
logger.addHandler(handler_log)
logger.setLevel(settings.log_level.upper())

def get_team_ids_for_pod(pod_name: str, mapping_file_path: str) -> list:
    if pod_name.startswith("dev") or "dyna" in pod_name:
        return []  # Skip dev and dyna-integration pods
    with open(mapping_file_path, "r") as f:
        mapping = json.load(f)

    matched_team_ids = []
    teams_services = mapping.get("teams_services", {})
    teams_ids = mapping.get("teams_ids", {})

    for team, services in teams_services.items():
        for service in services:
            if service in pod_name:
                team_id = teams_ids.get(team)
                if team_id and team_id not in matched_team_ids:
                    matched_team_ids.append(team_id)
                break  # Stop checking more services for this team

    return matched_team_ids

# ── 3) Slack SDK setup ───────────────────────────────────────────────
app = App(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
    ignoring_self_events_enabled=False,
)
socket_handler = SocketModeHandler(app, settings.slack_app_token)

# ── 4) Identify our bot_id ────────────────────────────────────────────
try:
    auth = app.client.auth_test()
    BOT_ID = auth["bot_id"]
    logger.info(f'{{"bot_id":"{BOT_ID}"}}')
except SlackApiError as e:
    BOT_ID = None
    logger.error(f'{{"auth_test_error":"{e.response["error"]}"}}')

# ── 5) Alert parsing setup ────────────────────────────────────────────
ALERT_PARSERS: Dict[str, Dict[str, Any]] = {
    "PodCrashLooping": {
        "fields":   ["cluster", "namespace", "pod", "container"],
        "endpoint":  settings.default_backend_url
    },
    "PodInErrorState": {
        "fields":   ["cluster", "namespace", "pod", "container"],
        "endpoint":  settings.default_backend_url
    },
    "PodPendingTooLong": {
        "fields":   ["cluster", "namespace", "pod", "container"],
        "endpoint":  settings.default_backend_url
    }
}

HYPERLINK_RE = re.compile(r"<(?:[^|>]+\|)?([^>]+)>")
RE_FIRING = re.compile(r"(\[FIRING[^\]]*\]|<[^|>]+\|\[FIRING[^\]]*\]>)", re.IGNORECASE)


def assemble_text(event: dict) -> str:

    attachments = event.get("attachments", []) 
    text = attachments[0].get("text", "") if attachments else ""
    details_blocks = re.findall(r'\*Details:\*([\s\S]*?)(?=\n\s*\n|\Z)', text)

    # Get the last details block
    if details_blocks:
        #logger.info(f'{{"details_blocks":"{details_blocks}"}}')
        last_details_block = details_blocks[0]
        # Extract desired fields using regex
        fields = {
            "alertname": re.search(r'\*alertname:\*\s*`([^`]*)`', last_details_block),
            "pod": re.search(r'\*pod:\*\s*`([^`]*)`', last_details_block),
            "cluster": re.search(r'\*cluster:\*\s*`<[^|>]+\|([^>]+)>`', last_details_block),
            "container": re.search(r'\*container:\*\s*`([^`]*)`', last_details_block),
            "namespace": re.search(r'\*namespace:\*\s*`([^`]*)`', last_details_block),
        }
    else:
        logger.info('{"assemble_text_error":"No details block found"}')
        return {}
    # Clean extracted fields
    details = {k: v.group(1) if v else None for k, v in fields.items()}
    return details

# ── 6) Backend call with retry ────────────────────────────────────────
@retry(
    reraise=True,
    stop=stop_after_attempt(settings.retry_attempts),
    wait=wait_exponential(
        multiplier=settings.retry_backoff_base,
        max=settings.retry_backoff_max
    ),
    retry=retry_if_exception_type(requests.RequestException)
)
def post_to_backend(url: str, payload: dict) -> dict:
    resp = requests.post(url, json=payload, timeout=200)
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not resp.ok:
        logger.error(f'{{"error":"{resp.status_code} {resp.reason}"}}')
        msg = (data or {}).get("detail") or (data or {}).get("error") or resp.text
        logger.error(f'{{"backend_error":"{msg}"}}')
        raise ValueError(f"Backend error: {msg}")
    return data or {}

# ── 7) “View Logs” button handler ────────────────────────────────────
@app.action("view_logs")
def handle_view_logs(ack, body: Dict[str, Any], client, say):
    ack()
    #logger.info(f'{{"action":"view_logs","body":{json.dumps(body, indent=2)}}}')
    payload = json.loads(body["actions"][0]["value"])
    channel = body["channel"]["id"]
    thread_ts = payload.get("thread_ts")
    user_id = body["user"]["id"]
    thread_ts = payload["thread_ts"]

    # Get the original alert timestamp from thread_ts
    alert_ts_epoch = float(thread_ts)
    alert_datetime = datetime.fromtimestamp(alert_ts_epoch, tz=timezone.utc)

    # Current time in UTC
    now_utc = datetime.now(timezone.utc)

    # Difference in time
    delta = now_utc - alert_datetime

    # If alert is older than 24 hours, notify user
    if delta.total_seconds() > 86400:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="⚠️ This alert is more than 24 hours old. " \
            " Currently logs can be fetched for past 24Hrs Only."
        )
        return
    # 1. Send loading GIF message first
    loading_msg = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f" Fetching logs...",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⏳ Logs on the way... *Ruko zara!* 😌"

                }
            }
        ]
    )

    try:
        # 2. Call backend to get logs
        result = post_to_backend(settings.default_backend_url, payload)
        logs = result.get("logs", "")
    except Exception as e:
        # Immediately remove loading GIF and show error message
        client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text=f"❌ Error fetching logs: `{e}`",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"❌ Something went wrong while fetching logs.\nError: `{str(e)}`"
                }
            }
        ]
        )
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".log") as tf:
        tf.write(logs.encode("utf-8"))
        filename = tf.name

    try:
        post_channel = settings.override_post_channel or channel
        client.files_upload_v2(
            channel=post_channel,
            file=filename,
            title=f"Full logs for {payload['pod']}",
            initial_comment=f"📂 Full logs for `{payload['pod']}`:",
            thread_ts=thread_ts
        )

        # 3. Update the loading message to show completion
        client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text=f"✅ Logs for `{payload['pod']}` have been uploaded.",
            blocks=[]
        )

        # 4. Set a timer to delete the file after 5 minutes (300 seconds)
        threading.Timer(300, os.remove, args=[filename]).start()  # 5 minutes delay

    except SlackApiError as e:
        client.chat_update(
            channel=channel,
            ts=loading_msg["ts"],
            text=f"⚠️ Couldn’t upload full logs: {e.response['error']}",
            blocks=[]
        )
# ── 8) Main event handler ─────────────────────────────────────────────
@app.event("message")
def handle_message(event: Dict[str,Any], say) -> None:
    if BOT_ID and event.get("bot_id") == BOT_ID:
        return
    #json_event = json.dumps(event, indent=2)
    #logger.info(f'{{"event":{json_event}}}')
    ts   = event["ts"]
    attachments = event.get("attachments", [])
    title = attachments[0].get("title", "") if attachments else ""
    text = attachments[0].get("text", "") if attachments else ""
    parsed_output = assemble_text(event)
    logger.info(f'{{"event_title":"{title}"}}')

   # Extract specific fields
    selected_details = {
        "alertname": parsed_output.get("alertname"),
        "pod": parsed_output.get("pod"),
        "cluster": parsed_output.get("cluster"),
        "namespace": parsed_output.get("namespace"),
        "container": parsed_output.get("container")
    }

    if not RE_FIRING.search(title):
        logger.info(f'{{"not_firing":"{title}"}}')
        return
    
    
    if selected_details['alertname'] not in settings.tracked_list:
        logger.info(f'{{"not_tracked_alertname":"{selected_details["alertname"]}"}}')
        return
    
    cfg, extracted, missing = ALERT_PARSERS[selected_details['alertname']], {}, []
    for key in cfg["fields"]:
        if key not in selected_details:
            missing.append(key)
        else:
            extracted[key] = selected_details[key]
    if missing:
        say(f"⚠️ Missing fields {missing} for alert `{selected_details['alertname']}`", thread_ts=ts)
        return
    
    payload = {
        **{k: extracted[k] for k in cfg["fields"]},
        "alertname": selected_details["alertname"],
        "thread_ts": ts,
        "channel":   event["channel"],
    }
    
    logger.info(f'{{"parsed_alert":{payload}}}')
    # ── JSON schema parsing ────────────────────────────────────────────
    try:
        back_response = post_to_backend(cfg["endpoint"], payload)
        #logger.debug(f'{{"backend_response":{back_response}}}')
        if not back_response:
            raise ValueError("No response from backend")
        if "error" in back_response:
            raise ValueError(f"Backend error: {back_response['error']}")

        llm_summary_str = back_response.get("llm_summary", "{}")
        logger.info(f'{{"llm_summary_str":{llm_summary_str}}}')
        try:
            summary_data = json.loads(llm_summary_str)
            #logger.info(f'{{"llm_summary":{summary_data}}}')
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in 'llm_summary': {e}")

        # Now safely extract fields
        tof = summary_data.get("type_of_failure", "Unknown Failure")
        analysis        = summary_data.get("analysis", "").strip()
        key_error_line  = summary_data.get("key_error_line", "").strip()
        summary_lines   = summary_data.get("summary", [])[:10]
        suggestions     = summary_data.get("suggestions", [])[:10]

    except Exception as e:
        err_str = str(e)
        logger.error(f'{{"❌ Error fetching summary":"{e}"}}')
            # Detect pod not found error and respond in thread
        if "Pod" in err_str and "failed to fetch logs from Loki" in err_str:
            try:
                test_grafana_url = "https://grafana.central.test.lan.didevops.com/d/platform-sre-logs/logs"
                prod_grafana_url = "https://grafana.central.adm.lan.didevops.com/d/platform-sre-logs/logs"
                post_channel = settings.override_post_channel or event["channel"]
                pod_name = payload.get('pod', 'unknown')
                cluster_name = payload.get('cluster', 'unknown')

                fallback = f"Pod {pod_name} logs unavailable"
                warning_text = (
                    f"⚠️ Pod `{pod_name}` not found or failed to fetch logs from Loki in cluster `{cluster_name}`.\n"
                    "Kindly refer to Grafana for detailed logs."
                )

                app.client.chat_postMessage(
                    channel=post_channel,
                    thread_ts=payload["thread_ts"],
                    attachments=[{
                    "fallback": fallback,
                    "color": "#AD6944",
                    "blocks": [
                        {"type":"section","text":{"type":"mrkdwn","text":f"⚠️ Pod `{pod_name}` not found in cluster `{cluster_name}` or failed to fetch logs from Loki. Check Grafana:"}},
                        {"type":"actions","elements":[
                        {"type":"button","text":{"type":"plain_text","text":"📊 Test Grafana","emoji":True},"url":test_grafana_url,"style":"primary"},
                        {"type":"button","text":{"type":"plain_text","text":"📊 Prod Grafana","emoji":True},"url":prod_grafana_url,"style":"primary"}
                        ]}
                    ]
                    }])
            except SlackApiError as slack_err:
                logger.error(f'Failed to send pod not found message: {slack_err.response["error"]}')
        elif "suspicious" in err_str or "sensitive information" in err_str:
            try:
                post_channel = settings.override_post_channel or event["channel"]
                app.client.chat_postMessage(
                    channel=post_channel,
                    text=(
                        f"⚠️ {err_str} "
                    ),
                    thread_ts=payload["thread_ts"]
                )
            except SlackApiError as slack_err:
                logger.error(f'Failed to send suspicious message: {slack_err.response["error"]}')
        return
    #Team_user = "U086WLDUR9N"    

    team_ids = get_team_ids_for_pod(extracted['pod'], "teams-services.json")
    if not team_ids:
        logger.info(f'{{"no_team_ids_found":"{extracted["pod"]}"}}')

    fallback = f"🚨 {tof}"
    # Build context elements list dynamically
    context_elements = [
        {"type": "mrkdwn", "text": f"*Cluster:* {extracted['cluster']}"},
        {"type": "mrkdwn", "text": f"*Namespace:* {extracted['namespace']}"},
        {"type": "mrkdwn", "text": f"*Pod:* {extracted['pod']}"}
    ]
    if team_ids:
        logger.info(f'{{"team_ids_found":"{team_ids}"}}')
        context_elements.append({"type": "mrkdwn", "text": f"*Team:* <!subteam^{team_ids[0]}>"} )

    message = {
        "fallback": fallback,
        "color": "#AD6944",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": fallback, "emoji": True}},
            {"type": "context", "elements": context_elements},
            {"type": "divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"*🔍 Analysis*"}},
            {"type":"section","text":{"type":"mrkdwn","text":analysis or "_No analysis provided._"}},
            *(
                [
                    {"type":"divider"},
                    {"type":"section","text":{"type":"mrkdwn","text":"*⛔ Error Line*"}},
                    {"type":"section","text":{"type":"mrkdwn","text":"```" + key_error_line + "```"}}
                ] if key_error_line else []
            ),
            *(
                [
                    {"type":"divider"},
                    {"type":"section","text":{"type":"mrkdwn","text":"*📋 Summary*"}},
                ] + [
                    {"type":"section","text":{"type":"mrkdwn","text":f"• {s}"}} 
                    for s in summary_lines
                ]
                if summary_lines else []
            ),
            *(
                [
                    {"type":"divider"},
                    {"type":"section","text":{"type":"mrkdwn","text":"*💡 Suggestions*"}},
                ] + [
                    {"type":"section","text":{"type":"mrkdwn","text":f"• {s}"}} 
                    for s in suggestions
                ]
                if suggestions else []
            ),
            {"type":"divider"},
            {"type":"actions","elements":[
                {"type":"button","text":{"type":"plain_text","text":"View Logs","emoji":True},
                 "action_id":"view_logs","value":json.dumps(payload),"style":"danger"},
                {"type": "button","text": { "type": "plain_text", "text": "📊 Test Grafana", "emoji": True },
                "url": "https://grafana.central.test.lan.didevops.com/d/platform-sre-logs/logs","style": "primary"},
                {"type": "button","text": { "type": "plain_text", "text": "📊 Prod Grafana", "emoji": True },
                "url": "https://grafana.central.adm.lan.didevops.com/d/platform-sre-logs/logs","style": "primary"}
            ]}
        ]
    }

    post_channel = settings.override_post_channel or event["channel"]
    logger.info(f'{{"post_channel":"{post_channel}"}}')
    try:
        app.client.chat_postMessage(
            channel=post_channel,
            text=fallback,
            attachments=[message],
            thread_ts=ts  # Optional: only include if threading is desired
        )
    except SlackApiError as e:
        logger.error(f"Failed to post message to Slack: {e.response['error']}")

# ── 9) Graceful shutdown ────────────────────────────────────────────
def shutdown(sig, _):
    logger.info(f'{{"shutdown":{sig}}}')
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ── 10) Entrypoint ──────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info('{"status":"starting"}')
    socket_handler.start()

   
