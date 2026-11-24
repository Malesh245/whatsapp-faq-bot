"""Simulate a WhatsApp customer chatting with the running server.

python scripts/simulate.py                      # scripted demo conversation
python scripts/simulate.py --interactive        # type messages yourself
python scripts/simulate.py --phone 918888888888 --url http://localhost:8000
"""

import argparse
import os
import time
import uuid

import httpx

DEMO = [
    "Hi",
    "What are your timings on Sunday?",
    "What is the price of A4 paper?",
    "What about the bigger size?",
    "Do you deliver outside the city?",
    "Do you sell printers?",  # not in FAQ -> handoff
    "My name is Priya, I want to order 4 reams of the A3 one",
    "Can I talk to a real person?",
]


def payload(phone: str, name: str, text: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [{"profile": {"name": name}, "wa_id": phone}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def chat(client: httpx.Client, args, text: str) -> None:
    resp = client.post(f"{args.url}/webhook", json=payload(args.phone, args.name, text))
    resp.raise_for_status()
    for r in resp.json()["processed"]:
        flags = []
        if r["needs_human"]:
            flags.append(f"NEEDS HUMAN: {r['handoff_reason']}")
        if r["lead_saved"]:
            flags.append("LEAD SAVED")
        print(
            f"👤 {text}\n🤖 {r['reply']}"
            + (f"   [{', '.join(flags)}]" if flags else "")
            + "\n"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--phone", default="919876543210")
    parser.add_argument("--name", default="Priya Sharma")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()

    with httpx.Client(timeout=60) as client:
        if args.interactive:
            print("Type a message (Ctrl+C to quit)\n")
            while True:
                text = input("> ").strip()
                if text:  # skip empty lines (just pressing Enter)
                    chat(client, args, text)
        else:
            for text in DEMO:
                chat(client, args, text)
            key = os.getenv("ADMIN_API_KEY")
            headers = {"X-API-Key": key} if key else {}
            print(
                "--- /leads ---\n",
                client.get(f"{args.url}/leads", headers=headers).json(),
            )
            print(
                "--- /handoffs ---\n",
                client.get(f"{args.url}/handoffs", headers=headers).json(),
            )


if __name__ == "__main__":
    main()
