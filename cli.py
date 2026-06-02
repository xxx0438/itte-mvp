import argparse
import json
import sys
import requests

def main():
    parser = argparse.ArgumentParser(
        description="ITTE CLI - pre-deployment AI risk gate"
    )

    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--environment", default="production")
    parser.add_argument("--change-type", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--diff-file", required=True)
    parser.add_argument("--metadata-file", default=None)

    args = parser.parse_args()

    with open(args.diff_file, "r", encoding="utf-8") as f:
        diff = f.read()

    metadata = {}

    if args.metadata_file:
        with open(args.metadata_file, "r", encoding="utf-8") as f:
            metadata = json.load(f)

    payload = {
        "repo": args.repo,
        "author": args.author,
        "environment": args.environment,
        "change_type": args.change_type,
        "title": args.title,
        "diff": diff,
        "metadata": metadata,
    }

    url = args.server.rstrip("/") + "/risk/evaluate"

    try:
        res = requests.post(url, json=payload, timeout=30)
    except requests.RequestException as e:
        print(f"ITTE error: failed to reach server: {e}", file=sys.stderr)
        sys.exit(2)

    if res.status_code >= 400:
        print(f"ITTE error: {res.status_code} {res.text}", file=sys.stderr)
        sys.exit(2)

    result = res.json()

    print("")
    print("========== ITTE Risk Gate ==========")
    print(f"Change ID : {result['change_id']}")
    print(f"Decision  : {result['decision'].upper()}")
    print(f"Risk Score: {result['risk_score']}")
    print("")
    print("Reasons:")
    for reason in result["reasons"]:
        print(f"- {reason}")

    if result["similar_incidents"]:
        print("")
        print("Similar historical incidents:")
        for item in result["similar_incidents"]:
            print(
                f"- Change #{item['change_id']} | "
                f"severity={item['severity']} | "
                f"similarity={item['similarity']} | "
                f"title={item['title']}"
            )

    print("====================================")
    print("")

    decision = result["decision"]

    if decision == "block":
        print("Deployment blocked by ITTE.")
        sys.exit(1)

    if decision == "review":
        print("Deployment requires human review.")
        sys.exit(1)

    print("Deployment allowed by ITTE.")
    sys.exit(0)

if __name__ == "__main__":
    main()
