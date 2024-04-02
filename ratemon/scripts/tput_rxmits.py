#!/usr/bin/env python3
"""
Parses iperf3 sender-side JSON output file generated by cctestbedv2.py.
"""

import argparse
import json
import sys


def parse_args():
    parser = argparse.ArgumentParser(
        description="Calculate average throughput and retransmission count"
    )
    parser.add_argument(
        "--in-file",
        type=str,
        help="Input JSON file (output of iperf3 sender).",
        required=True,
    )
    parser.add_argument("--out-file", type=str, help="Output file.", required=True)
    return parser.parse_args()


def main(args):
    with open(args.in_file, "r", encoding="utf-8") as fil:
        res = json.load(fil)

    if "end" not in res:
        print(f"Error: 'end' key not found in JSON file: {args.in_file}")
        return 1

    bps = res["end"]["sum_sent"]["bits_per_second"]
    rxmits = res["end"]["sum_sent"]["retransmits"]
    msg = f"Throughput: {bps / 1e9:.2f} Gbps\nRetransmits: {rxmits}"
    print(msg)
    with open(args.out_file, "w", encoding="utf-8") as fil:
        fil.write(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main(parse_args()))
