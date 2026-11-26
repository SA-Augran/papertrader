"""Minimal existing-Agent host. No account database or provider key is needed here."""
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def call(path, data=None):
    request = Request(os.getenv("PAPERTRADER_URL", "http://127.0.0.1:8322") + path,
                      headers={"Authorization": "Bearer " + os.environ["PAPERTRADER_TOKEN"],
                               "Content-Type": "application/json"},
                      data=json.dumps(data).encode() if data is not None else None)
    with urlopen(request, timeout=45) as response:
        return json.load(response)


def your_agent(context):
    # Replace this function with your host's model/tool loop.
    # The sample observes instead of placing an unsolicited order.
    return {"action": "hold", "code": "", "weight": 0, "reason": "收到当日事实，先观察。"}


if __name__ == "__main__":
    try:
        task = call("/api/context")
        # Persist task_id + proposal on the host if the network may disconnect.
        # Retry the SAME proposal for this task instead of asking the model again.
        proposal = your_agent(task)
        print(json.dumps(call("/api/submit", {"task_id": task["task_id"], "proposal": proposal}),
                         ensure_ascii=False, indent=2))
    except HTTPError as error:
        print(json.load(error))  # Outside the window, stale quote or finalized task.
