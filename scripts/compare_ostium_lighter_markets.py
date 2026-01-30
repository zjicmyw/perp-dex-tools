import argparse
import asyncio
import json
import os
import sys
from typing import Iterable, Optional, Set

import requests


def _normalize_symbol(value: str) -> str:
    return value.strip().upper().replace("/", "-").replace("_", "-")


def _normalize_pair(from_asset: str, to_asset: str) -> str:
    return _normalize_symbol(f"{from_asset}-{to_asset}")


def _fetch_lighter_symbols(base_url: str, timeout: int) -> Set[str]:
    url = f"{base_url.rstrip('/')}/api/v1/orderBooks"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    order_books = data.get("order_books") or data.get("orderBooks") or []
    symbols = set()
    for item in order_books:
        symbol = item.get("symbol") or item.get("pair") or item.get("name")
        if symbol:
            symbols.add(_normalize_symbol(symbol))
    return symbols


async def _fetch_ostium_symbols(
    network: str,
    rpc_url: str,
    private_key: Optional[str],
    debug: bool,
) -> Set[str]:
    try:
        from ostium_python_sdk import OstiumSDK, NetworkConfig
    except Exception as exc:
        raise RuntimeError(
            "ostium-python-sdk is not installed. Run: pip install ostium-python-sdk"
        ) from exc

    if network == "mainnet":
        config = NetworkConfig.mainnet()  # type: ignore[attr-defined]
    else:
        config = NetworkConfig.testnet()

    sdk = OstiumSDK(config, private_key, rpc_url)
    pairs = await sdk.subgraph.get_pairs()

    # Some SDKs return a dict wrapper (e.g. {"pairs": [...]}) or nested data.
    if isinstance(pairs, dict):
        for key in ("pairs", "data", "result"):
            if key in pairs and isinstance(pairs[key], list):
                pairs = pairs[key]
                break

    if debug:
        try:
            preview = pairs if isinstance(pairs, list) else {"raw": pairs}
            print("[debug] Ostium get_pairs() preview:")
            print(json.dumps(preview[:5] if isinstance(preview, list) else preview, indent=2))
        except Exception as exc:
            print(f"[debug] Failed to print Ostium preview: {exc}")

    symbols = set()
    for item in pairs or []:
        symbol = (
            item.get("pair")
            or item.get("symbol")
            or item.get("pair_name")
            or item.get("name")
        )
        if symbol:
            symbols.add(_normalize_symbol(symbol))
            continue

        from_asset = item.get("from")
        to_asset = item.get("to")
        if from_asset and to_asset:
            symbols.add(_normalize_pair(from_asset, to_asset))
    return symbols


def _lighter_pair_to_base_quote(symbol: str) -> Optional[str]:
    """Lighter 货币对多为 6 位无连字符（如 EURUSD、USDJPY），转为 BASE-QUOTE 与 Ostium 一致。"""
    s = symbol.strip().upper()
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}-{s[3:]}"
    return None


def _format_list(values: Iterable[str]) -> str:
    return "\n".join(sorted(values))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Ostium and Lighter symbols and print overlap"
    )
    parser.add_argument(
        "--network",
        choices=["mainnet", "testnet"],
        default="mainnet",
        help="Ostium network",
    )
    parser.add_argument(
        "--rpc-url",
        default=os.getenv("RPC_URL", ""),
        help="RPC URL for Ostium (required)",
    )
    parser.add_argument(
        "--private-key",
        default=os.getenv("PRIVATE_KEY"),
        help="Ostium private key (optional, read-only without it)",
    )
    parser.add_argument(
        "--lighter-base-url",
        default=os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"),
        help="Lighter REST base URL",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--debug-ostium",
        action="store_true",
        help="Print a preview of Ostium get_pairs() response",
    )
    args = parser.parse_args()

    if not args.rpc_url:
        print("RPC_URL is required for Ostium SDK (set env or pass --rpc-url).")
        return 2

    try:
        lighter_symbols = _fetch_lighter_symbols(args.lighter_base_url, args.timeout)
    except Exception as exc:
        print(f"Failed to fetch Lighter symbols: {exc}")
        return 1

    try:
        ostium_symbols = asyncio.run(
            _fetch_ostium_symbols(
                args.network, args.rpc_url, args.private_key, args.debug_ostium
            )
        )
    except Exception as exc:
        print(f"Failed to fetch Ostium symbols: {exc}")
        return 1

    common_exact = lighter_symbols.intersection(ostium_symbols)
    only_lighter = lighter_symbols.difference(ostium_symbols)
    only_ostium = ostium_symbols.difference(lighter_symbols)

    # Ostium 多为 XXX-USD / XXX-EUR 等，取 base 与 Lighter 的单一符号做“相同标的”对比
    ostium_bases: Set[str] = set()
    for s in ostium_symbols:
        if "-" in s:
            ostium_bases.add(s.split("-")[0])
        else:
            ostium_bases.add(s)
    common_by_base = lighter_symbols.intersection(ostium_bases)

    # Lighter 货币对为 6 位无连字符（EURUSD、USDJPY），转成 XXX-YYY 与 Ostium 的 EUR-USD、USD-JPY 对比
    lighter_as_pair: Set[str] = set()
    for s in lighter_symbols:
        pair_form = _lighter_pair_to_base_quote(s)
        if pair_form:
            lighter_as_pair.add(pair_form)
    common_forex = ostium_symbols.intersection(lighter_as_pair)

    # 相同标的 = 按 base 的单一资产 + 货币对（Ostium 形式列出）
    common_all_symbols = common_by_base.union(common_forex)

    print(f"Lighter symbols: {len(lighter_symbols)}")
    print(f"Ostium symbols: {len(ostium_symbols)}")
    print(f"Common (exact match): {len(common_exact)}")
    print(f"Common (by base asset): {len(common_by_base)}")
    print(f"Common (forex pairs, 货币对): {len(common_forex)}")
    print(f"Common (all 相同标的): {len(common_all_symbols)}")
    print("\n== Common (exact) ==")
    print(_format_list(common_exact))
    print("\n== Common (by base asset) ==")
    print(_format_list(common_by_base))
    print("\n== Common (forex pairs 货币对) ==")
    print(_format_list(common_forex))
    print("\n== Common (all 相同标的 = base + 货币对) ==")
    print(_format_list(sorted(common_all_symbols)))
    print("\n== Only Lighter ==")
    print(_format_list(only_lighter))
    print("\n== Only Ostium ==")
    print(_format_list(only_ostium))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
