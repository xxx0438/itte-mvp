import argparse
import json
import sys
import requests

def main():
    parser = argparse.ArgumentParser(description="ITTE CLI risk gate")

    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--org", default="default")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--environment", default="production")
    parser.add_argument("--change-type", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--diff-file", required=True)
    parser.add_argument("--metadata-file")

    args = parser.parse_args()

    with open(args.diff_file, "r", encoding="utf-8") as f:
        diff = f.read()

    metadata = {}

    if args.metadata_file:
        with open(args.metadata_file, "r", encoding="utf-8") as f:
            metadata = json.load(f)

    payload = {
        "org": args.org,
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
        res = requests.post(url, json=payload, timeout=90)
    except requests.RequestException as e:
        print(f"ITTE error: {e}", file=sys.stderr)
        sys.exit(2)

    if res.status_code >= 400:
        print(res.text, file=sys.stderr)
        sys.exit(2)

    data = res.json()

    print("\n========== ITTE Risk Gate ==========")
    print(f"Change ID : {data['change_id']}")
    print(f"Decision  : {data['decision'].upper()}")
    print(f"Risk Score: {data['risk_score']}")

    if data.get("approval_id"):
        print(f"Approval  : #{data['approval_id']}")

    print("\nReasons:")
    for reason in data["reasons"]:
        print(f"- {reason}")

    if data["compliance_findings"]:
        print("\nCompliance Findings:")
        for item in data["compliance_findings"]:
            print(
                f"- [{item['framework']}] "
                f"{item['severity']} | {item['title']}"
            )

    if data["similar_memory"]:
        print("\nSimilar Memory:")
        for item in data["similar_memory"]:
            print(
                f"- memory #{item['memory_id']} | "
                f"source={item['source']} | "
                f"severity={item['severity']} | "
                f"similarity={item['similarity']} | "
                f"boost={item['risk_boost']}"
            )

    print("====================================\n")

    if data["decision"] == "block":
        print("Deployment blocked by ITTE.")
        sys.exit(1)

    if data["decision"] == "review":
        print("Deployment requires human review.")
        sys.exit(1)

    print("Deployment allowed by ITTE.")
    sys.exit(0)

if __name__ == "__main__":
    main()
