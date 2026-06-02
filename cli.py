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
        "metadata": metadata
    }

    url = args.server.rstrip("/") + "/risk/evaluate"

    try:
        res = requests.post(url, json=payload, timeout=60)
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
    for r in data["reasons"]:
        print(f"- {r}")

    if data["compliance_findings"]:
        print("\nCompliance Findings:")
        for f in data["compliance_findings"]:
            print(f"- [{f['framework']}] {f['severity']} | {f['title']}")

    if data["similar_memory"]:
        print("\nSimilar Memory:")
        for m in data["similar_memory"]:
            print(
                f"- memory #{m['memory_id']} | "
                f"source={m['source']} | "
                f"severity={m['severity']} | "
                f"similarity={m['similarity']} | "
                f"boost={m['risk_boost']}"
            )

    print("====================================\n")

    if data["decision"] in ["review", "block"]:
        sys.exit(1)

    sys.exit(0)

if __name__ == "__main__":
    main()
