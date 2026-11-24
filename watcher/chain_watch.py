#!/usr/bin/env python3
"""USDC 四池盯链机器人（另外盯 USDe 脱锚和 Aave 上的 USDe 抵押规模）。

由 GitHub Actions 每 10 分钟运行一次，读取以太坊主网上 4 个 USDC 池子的链上数据，
发现异常时通过 Bark 推送到手机。只用 Python 标准库。

推送密钥从环境变量 BARK_KEY 读取（GitHub Secrets），代码和日志里都不会出现。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ---------- 监控对象 ----------
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
AAVE_ATOKEN = "0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c"  # Aave V3 aEthUSDC
COMET = "0xc3d688B66703497DAA19211EEdff47f25384cdc3"        # Compound V3 cUSDCv3
MORPHO_VAULTS = {
    "steak": ("Morpho Steakhouse Prime", "0xbeef088055857739C12CD3765F20b7679Def0f51"),
    "gaunt": ("Morpho Gauntlet Prime", "0x8c106EEDAd96553e64287A5A6839c3Cc78afA3D0"),
}
POOL_NAMES = {"aave": "Aave V3", "comp": "Compound V3", **{k: v[0] for k, v in MORPHO_VAULTS.items()}}

# USDe：Aave 上最大的一类稳定币抵押品（循环借贷借走的是 USDC），脱锚时 Aave USDC 池可能出坏账
USDE_CURVE = "0x02950460E2b9529D0E00284A5fA2d7bDF3fA4d72"   # Curve USDe/USDC 池（coin0=USDe, coin1=USDC）
A_USDE = "0x4F5923Fc5FD4a93352581b38B7cD26943012DECF"       # Aave V3 aEthUSDe
A_SUSDE = "0x4579a27aF00A62C0EB156349f31B345c08386419"      # Aave V3 aEthsUSDe
SUSDE = "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"
EXTRA_NAMES = {"usde": "USDe"}

RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://cloudflare-eth.com",
    "https://rpc.ankr.com/eth",
]
MORPHO_API = "https://api.morpho.org/graphql"
PAGE = "https://maozedong719-cell.github.io/invest-check/#usdc"

# ---------- 阈值 ----------
LIQ_WARN, LIQ_CRIT = 0.05, 0.02          # 可取现金 / 总存款
OUT_1H_WARN, OUT_1H_CRIT = 0.08, 0.15    # 1 小时内存款从高点流出比例
OUT_24H_WARN = 0.20                      # 24 小时内存款从高点流出比例
SHARE_DROP = 0.0005                      # Morpho 份额价格下跌 0.05% 以上 = 坏账落地
SAFE_COLLATERAL = {"cbBTC", "WBTC", "wstETH", "weETH", "WETH"}
UNSAFE_MIN_USD = 50_000                  # 白名单外抵押品市场分到这么多钱才报警
USDE_WARN, USDE_CRIT = 0.99, 0.97       # Curve 上卖 10 万 USDe 换到的 USDC 单价
USDE_COLL_GROW = 0.15                    # Aave 的 USDe 系抵押品 24 小时增长 15% 以上
USDE_COLL_STEP = 2.5e8                   # 抵押品规模每跨过一个 2.5 亿台阶（从 15 亿起）提醒一次
USDE_COLL_FLOOR = 1.5e9
FAIL_LIMIT = 3                           # 连续读取失败几次才报警
REMIND = {"warn": 6 * 3600, "crit": 2 * 3600}  # 异常持续时多久再提醒一次
LEVEL_RANK = {"warn": 1, "crit": 2}
HEARTBEAT_HOUR_HK = 9                    # 每天香港时间 9 点后发一条"运行正常"

HK = dt.timezone(dt.timedelta(hours=8))


# ---------- 网络 ----------
def post_json(url, payload, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "user-agent": "chain-watch/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def eth_call(to, data):
    last = None
    for rpc in RPCS:
        try:
            r = post_json(rpc, {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                "params": [{"to": to, "data": data}, "latest"]})
            if "result" in r and r["result"] not in (None, "0x"):
                return int(r["result"], 16)
            last = r.get("error")
        except Exception as e:  # 换下一个节点
            last = e
    raise RuntimeError(f"所有 RPC 都失败：{last}")


def balance_of(token, holder):
    return eth_call(token, "0x70a08231" + holder[2:].lower().rjust(64, "0"))


def total_supply(token):
    return eth_call(token, "0x18160ddd")


def gql(query):
    r = post_json(MORPHO_API, {"query": query})
    if r.get("errors"):
        raise RuntimeError(r["errors"][0].get("message"))
    return r["data"]


# ---------- 读取四个池子 ----------
def read_aave():
    cash = balance_of(USDC, AAVE_ATOKEN) / 1e6
    tvl = total_supply(AAVE_ATOKEN) / 1e6
    return {"tvl": tvl, "liq": cash}


def read_comp():
    cash = balance_of(USDC, COMET) / 1e6
    tvl = total_supply(COMET) / 1e6
    return {"tvl": tvl, "liq": min(cash, tvl)}


def read_morpho():
    parts = []
    for key, (_, addr) in MORPHO_VAULTS.items():
        parts.append(
            f'{key}: vaultV2ByAddress(address:"{addr}", chainId:1){{ totalAssetsUsd liquidityUsd sharePrice '
            f'adapters{{ items{{ address }} }} pendingConfigs{{ items{{ functionName validAt txHash }} }} }}'
        )
    data = gql("{ " + " ".join(parts) + " }")
    out, adapter_owner = {}, {}
    for key in MORPHO_VAULTS:
        v = data[key]
        out[key] = {
            "tvl": v["totalAssetsUsd"],
            "liq": v["liquidityUsd"],
            "share": v["sharePrice"],
            "pending": [p for p in v["pendingConfigs"]["items"]],
            "unsafe": [],
        }
        for a in v["adapters"]["items"]:
            adapter_owner[a["address"].lower()] = key
    if adapter_owner:
        addrs = ",".join(f'"{a}"' for a in adapter_owner)
        pos = gql(
            f"{{ marketPositions(first:200, where:{{userAddress_in:[{addrs}], chainId_in:[1]}}){{ items{{ "
            f"user{{address}} market{{ marketId collateralAsset{{symbol}} }} state{{ supplyAssetsUsd }} }} }} }}"
        )["marketPositions"]["items"]
        for p in pos:
            usd = p["state"]["supplyAssetsUsd"] or 0
            coll = (p["market"]["collateralAsset"] or {}).get("symbol")
            if coll and coll not in SAFE_COLLATERAL and usd >= UNSAFE_MIN_USD:
                key = adapter_owner.get(p["user"]["address"].lower())
                if key:
                    out[key]["unsafe"].append((coll, usd))
    return out


def read_usde():
    sell = 100_000 * 10**18
    out = eth_call(USDE_CURVE, "0x5e0d443f" + "0" * 64 + "1".rjust(64, "0") + hex(sell)[2:].rjust(64, "0"))
    price = out / 1e6 / 100_000
    # sUSDe 按金库汇率折成 USDe（convertToAssets(1e18)），再按 1 美元计
    rate = eth_call(SUSDE, "0x07a2d13a" + hex(10**18)[2:].rjust(64, "0")) / 1e18
    coll = total_supply(A_USDE) / 1e18 + total_supply(A_SUSDE) / 1e18 * rate
    return {"price": price, "coll": coll}


def read_all():
    """返回 {pool: data} 和 {pool: 错误信息}。"""
    res, errs = {}, {}
    for key, fn in (("aave", read_aave), ("comp", read_comp), ("usde", read_usde)):
        try:
            res[key] = fn()
        except Exception as e:
            errs[key] = str(e)
    try:
        res.update(read_morpho())
    except Exception as e:
        for key in MORPHO_VAULTS:
            errs[key] = str(e)
    return res, errs


# ---------- 判断 ----------
def fmt_usd(x):
    if x >= 1e8:
        return f"{x / 1e8:.2f} 亿美元"
    if x >= 1e4:
        return f"{x / 1e4:,.0f} 万美元"
    return f"{x:,.0f} 美元"


def peak_since(hist, now, seconds):
    vals = [v for t, v in hist if t >= now - seconds]
    return max(vals) if vals else None


def evaluate(res, errs, state, now):
    """返回 (持续性异常 {id: (level, title, body)}, 一次性事件 [(level, title, body)])。"""
    active, events = {}, []
    hist = state.setdefault("hist", {})
    shares = state.setdefault("share", {})
    fails = state.setdefault("fail", {})
    seen_pending = state.setdefault("pending", {})

    for key in POOL_NAMES:
        name = POOL_NAMES[key]
        if key in errs:
            fails[key] = fails.get(key, 0) + 1
            if fails[key] >= FAIL_LIMIT:
                active[f"data:{key}"] = ("warn", f"{name} 数据读取失败",
                                         f"已连续 {fails[key]} 次读不到数据，请打开检查页手动确认。")
            continue
        fails[key] = 0
        d = res[key]
        tvl, liq = d["tvl"], d["liq"]

        # 1) 可取现金比例
        ratio = liq / tvl if tvl else 0
        if ratio < LIQ_WARN:
            lvl = "crit" if ratio < LIQ_CRIT else "warn"
            active[f"liq:{key}"] = (lvl, f"{name} 可取现金只剩 {ratio:.1%}",
                                    f"存款 {fmt_usd(tvl)}，现在能取出的只有 {fmt_usd(liq)}。"
                                    "利用率接近 100% 时存款人会排队取不出钱。")

        # 2) 存款快速流出
        h = hist.setdefault(key, [])
        p1 = peak_since(h, now, 3900)
        p24 = peak_since(h, now, 24 * 3600 + 600)
        if p1 and tvl < p1:
            drop = 1 - tvl / p1
            if drop >= OUT_1H_WARN:
                lvl = "crit" if drop >= OUT_1H_CRIT else "warn"
                active[f"out1h:{key}"] = (lvl, f"{name} 1 小时内存款流出 {drop:.1%}",
                                          f"从 {fmt_usd(p1)} 降到 {fmt_usd(tvl)}，可能有大户在撤离。")
        if p24 and tvl < p24:
            drop = 1 - tvl / p24
            if drop >= OUT_24H_WARN:
                active[f"out24h:{key}"] = ("warn", f"{name} 24 小时内存款流出 {drop:.1%}",
                                           f"从 {fmt_usd(p24)} 降到 {fmt_usd(tvl)}。")
        h.append([now, tvl])
        hist[key] = [x for x in h if x[0] >= now - 26 * 3600]

        if key not in MORPHO_VAULTS:
            continue

        # 3) Morpho 份额价格下跌 = 坏账已经记到存款人头上
        sp, last = d["share"], shares.get(key)
        if last and sp < last * (1 - SHARE_DROP):
            events.append(("crit", f"{name} 份额价格下跌 {1 - sp / last:.2%}",
                           f"份额价格从 {last:.6f} 跌到 {sp:.6f}，通常表示金库出现了坏账损失。"))
        shares[key] = sp

        # 4) 借给了白名单外的抵押品
        if d["unsafe"]:
            items = "、".join(f"{c} {fmt_usd(u)}" for c, u in d["unsafe"])
            active[f"unsafe:{key}"] = ("crit", f"{name} 出现非主流抵押品",
                                       f"金库把钱借给了：{items}。平时只应该是 BTC/ETH 类抵押品。")

        # 5) 金库经理排队中的配置变更（Morpho V2 有时间锁，生效前就能看到）
        ids = {p["txHash"] + p["functionName"] for p in d["pending"]}
        new = [p for p in d["pending"] if p["txHash"] + p["functionName"] not in seen_pending.get(key, [])]
        if key in seen_pending and new:
            lines = []
            for p in new:
                when = dt.datetime.fromtimestamp(int(p["validAt"]), HK).strftime("%m-%d %H:%M")
                lines.append(f"{p['functionName']}（香港时间 {when} 后可生效）")
            events.append(("warn", f"{name} 有排队中的配置变更",
                           "金库经理提交了：" + "；".join(lines) + "。可能是新增借贷市场或调整额度，建议看一眼。"))
        seen_pending[key] = sorted(ids)

    # 6) USDe 脱锚、Aave 上 USDe 循环借贷继续加码
    if "usde" in errs:
        fails["usde"] = fails.get("usde", 0) + 1
        if fails["usde"] >= FAIL_LIMIT:
            active["data:usde"] = ("warn", "USDe 数据读取失败",
                                   f"已连续 {fails['usde']} 次读不到 USDe 价格或 Aave 抵押规模。")
    elif "usde" in res:
        fails["usde"] = 0
        u = res["usde"]
        if u["price"] < USDE_WARN:
            lvl = "crit" if u["price"] < USDE_CRIT else "warn"
            active["peg:usde"] = (lvl, f"USDe 脱锚：{u['price']:.4f}",
                                  f"在 Curve 上卖 10 万 USDe 只能换到 {u['price'] * 100_000:,.0f} USDC。"
                                  f"Aave 上有 {fmt_usd(u['coll'])} 的 USDe 系抵押品借走了 USDC/USDT，"
                                  "脱锚持续会让 Aave USDC 池出现坏账或取不出钱，请留意 Aave 可取现金。")
        h = hist.setdefault("usde", [])
        old = [v for t, v in h if now - 24 * 3600 - 1800 <= t <= now - 23 * 3600]
        if old and u["coll"] > old[0] * (1 + USDE_COLL_GROW):
            active["grow:usde"] = ("warn", f"Aave 上 USDe 抵押 24 小时增加 {u['coll'] / old[0] - 1:.0%}",
                                   f"从 {fmt_usd(old[0])} 增加到 {fmt_usd(u['coll'])}，循环借贷还在加码，"
                                   "借走的 USDC 越多，USDe 出事时 Aave 的风险越大。")
        h.append([now, u["coll"]])
        hist["usde"] = [x for x in h if x[0] >= now - 26 * 3600]
        band = int(u["coll"] // USDE_COLL_STEP) if u["coll"] >= USDE_COLL_FLOOR else 0
        prev_band = state.get("usde_band")
        if prev_band is not None and band > prev_band:
            events.append(("warn", f"Aave 上 USDe 抵押超过 {band * USDE_COLL_STEP / 1e8:.1f} 亿美元",
                           f"现在是 {fmt_usd(u['coll'])}（11/24 时约 13 亿）。规模越大，USDe 出事对 Aave USDC 池的冲击越大。"))
        state["usde_band"] = band

    return active, events


def summary(res, errs):
    lines = []
    for key, name in POOL_NAMES.items():
        if key in errs:
            lines.append(f"{name}：读取失败")
            continue
        d = res[key]
        ratio = d["liq"] / d["tvl"] if d["tvl"] else 0
        lines.append(f"{name}：存款 {fmt_usd(d['tvl'])}，可取 {ratio:.0%}")
    if "usde" in errs:
        lines.append("USDe：读取失败")
    elif "usde" in res:
        lines.append(f"USDe：Curve 价格 {res['usde']['price']:.4f}，Aave 抵押 {fmt_usd(res['usde']['coll'])}")
    return "\n".join(lines)


# ---------- 推送 ----------
def bark_key():
    raw = os.environ.get("BARK_KEY", "").strip()
    if "/" in raw:  # 用户可能直接粘贴了 https://api.day.app/xxxx/ 这样的地址
        parts = [p for p in raw.split("/") if p and "." not in p and p not in ("https:", "http:")]
        raw = parts[0] if parts else ""
    return raw


def redact(text, key):
    text = str(text)
    if key:
        text = text.replace(key, "***")
    return text[:200]


def push(level, title, body, dry):
    bark_level = {"crit": "critical", "warn": "timeSensitive", "info": "active", "quiet": "passive"}[level]
    tag = {"crit": "【紧急】", "warn": "【注意】", "info": "", "quiet": ""}[level]
    print(f"[推送 {level}] {tag}{title}\n  {body.replace(chr(10), chr(10) + '  ')}")
    if dry:
        return
    payload = {"device_key": bark_key(), "title": tag + title, "body": body,
               "group": "盯链机器人", "url": PAGE, "level": bark_level}
    if level == "crit":
        payload.update({"sound": "alarm", "volume": 8})
    for attempt in range(3):
        try:
            r = post_json("https://api.day.app/push", payload)
            if r.get("code") == 200:
                return
            print("  Bark 返回：", redact(r.get("message", ""), payload["device_key"]))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("message", "")
            except Exception:
                msg = ""
            # 日志是公开的，Bark 的报错里会带上密钥，必须先抹掉
            print(f"  Bark 发送失败：HTTP {e.code} {redact(msg, payload['device_key'])}；密钥长度 {len(payload['device_key'])}")
        except Exception as e:
            print("  Bark 发送失败：", type(e).__name__)
        time.sleep(2)


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="watch-state.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发推送")
    ap.add_argument("--test", action="store_true", help="发一条测试推送")
    ap.add_argument("--test-level", choices=["info", "warn", "crit"], default="info",
                    help="测试推送的级别：info 普通 / warn 时效性 / crit 重要警告（无视静音）")
    args = ap.parse_args()

    if not args.dry_run and not bark_key():
        print("没有设置 BARK_KEY，请在仓库 Settings → Secrets and variables → Actions 里添加。")
        sys.exit(1)

    try:
        with open(args.state, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    first_run = not state

    now = int(time.time())
    res, errs = read_all()
    active, events = evaluate(res, errs, state, now)
    print(summary(res, errs))

    if args.test and args.test_level != "info":
        push(args.test_level, "测试推送（不是真实报警）",
             "这是一条测试，用来确认这个级别的通知会不会响铃和弹出。\n" + summary(res, errs), args.dry_run)
    elif args.test or first_run:
        push("info", "盯链机器人已连接" if first_run else "测试推送",
             "每 10 分钟检查一次 4 个 USDC 池子和 USDe，有异常会推送到这里。\n" + summary(res, errs), args.dry_run)

    alerts = state.setdefault("alerts", {})
    for aid, (lvl, title, body) in active.items():
        prev = alerts.get(aid)
        due = (prev is None or LEVEL_RANK[lvl] > LEVEL_RANK[prev["level"]]
               or now - prev["sent"] >= REMIND[lvl])
        if due:
            push(lvl, title, body, args.dry_run)
            alerts[aid] = {"level": lvl, "sent": now, "title": title}
        elif LEVEL_RANK[lvl] < LEVEL_RANK[prev["level"]]:
            alerts[aid]["level"] = lvl
    for aid in [a for a in alerts if a not in active]:
        k = aid.split(":", 1)[1]
        pool = POOL_NAMES.get(k) or EXTRA_NAMES.get(k, "")
        push("info", f"{pool} 已恢复正常", f"之前的异常：{alerts[aid]['title']}。现在已经解除。", args.dry_run)
        del alerts[aid]
    for lvl, title, body in events:
        push(lvl, title, body, args.dry_run)

    today = dt.datetime.now(HK)
    if (not first_run and today.hour >= HEARTBEAT_HOUR_HK
            and state.get("heartbeat") != today.strftime("%Y-%m-%d")):
        status = "目前没有异常。" if not alerts else f"有 {len(alerts)} 项异常仍未解除。"
        push("quiet", "盯链机器人运行正常", status + "\n" + summary(res, errs), args.dry_run)
        state["heartbeat"] = today.strftime("%Y-%m-%d")
    elif first_run:
        state["heartbeat"] = today.strftime("%Y-%m-%d") if today.hour >= HEARTBEAT_HOUR_HK else ""

    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
