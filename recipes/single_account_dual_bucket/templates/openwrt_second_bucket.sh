#!/bin/sh
# =============================================================================
# 模板：OpenWrt 第二桶配置脚本（单账号双桶 · 脱敏模板）
#
# 【用途】
#   在 OpenWrt 上把你账号白名单里第二台"真实离线"设备的 MAC 克隆到一个 WAN 口，
#   触发无感（免密）放行形成第二个独立会话（桶2），并对外提供：
#      · 一个 SOCKS 出口（供宿主机把流量塞进桶2）；
#      · （可选）把该 WAN 口加进"内核 multipath 默认路由 + L4 哈希"的分桶池，
#        让多 WAN 腿按流自动分担。
#   宿主机侧再用聚合代理把桶1（直连本体）+ 桶2（本脚本的 SOCKS）合并（见
#   host_aggregate.yaml）。
#
# 【重要：合规边界】
#   · 只能克隆**你账号白名单里、你拥有且真实离线**的设备的 MAC；
#   · 绝不克隆他人设备的 MAC，不使用他人账号；
#   · 克隆目标真机必须离线（否则会互顶 / 冲突，见仓库 docs/04 的 F3 故障模式）；
#   · 仅限在你有使用权的设备 / 账号上实验，遵守所在网络的接入与使用规定。
#
# 【前置条件】
#   · OpenWrt（含 uci / ip / curl），root 执行；
#   · 已安装 microsocks（SOCKS 服务）：opkg update && opkg install microsocks
#     （若用其它 SOCKS 程序，改 SOCKSBIN 与启动参数）；
#   · OpenWrt VM 已桥接到与墙口同一网段（DHCP 能拿到校园内网 IP）；
#   · 你账号有 ≥2 并发名额、且白名单里有第二台可离线设备（见 setup.md §0）。
#
# 【占位符清单】
#   <物理WAN接口>     OpenWrt 上承载桶2的 WAN 网口名（已桥接、能 DHCP）
#   <MAC_B>           你账号白名单里第二台"离线设备"的 MAC（克隆目标）
#   <网关IP>          上游网关地址
#   <内网IP_B>        桶2 会话在本机的源地址（DHCP 拿到后自动填入，见脚本内）
#   <探活目标URL>      无感放行连通性探测（204 类，如 connect 检测地址）
#   <探活目标IP>       multipath 追踪 / ping 探测目标（公共 DNS 类）
#   <SOCKS端口-B>      桶2 的 SOCKS 监听端口（如 1080）
#   <SOCKS监听地址>    SOCKS 监听地址（一般通配监听，配合 -b 绑定源地址）
#
# 【风险提示】
#   · 克隆 MAC / 多会话会改变"设备数"表象，可能违反接入协议条款，后果自负；
#   · 任何 MAC / 接口变动都有端口级成本：单点、低频、一次做完、长期冻结；
#     频繁变动可能触发上游对"多 MAC + 高频 DHCP"的防护，导致会话被拒甚至端口
#     冷却（见 docs/04）。失败后冷却 ≥30 分钟，只读轮询，别反复重试；
#   · 内存态配置（路由 / SOCKS 进程）重启后失效，需按末尾提示持久化；
#   · 本模板不含任何真实标识；占位符规范见 docs/00_脱敏与贡献规范.md。
# =============================================================================
set -e

# ------------------------------ 占位符替换区 --------------------------------
WAN_IF="<物理WAN接口>"
CLONE_MAC="<MAC_B>"
GW_IP="<网关IP>"
PROBE_URL="<探活目标URL>"
TRACK_IP="<探活目标IP>"
SOCKS_PORT="<SOCKS端口-B>"
SOCKS_LISTEN="<SOCKS监听地址>"
SOCKSBIN="/usr/bin/microsocks"
# ----------------------------------------------------------------------------

echo "=== [1/4] 克隆你账号离线设备 MAC 并 DHCP ==="
uci set network.wan2.device="$WAN_IF"
uci set network.wan2.proto='dhcp'
uci set network.wan2.macaddr="$CLONE_MAC"
uci commit network
ifup wan2
sleep 10
ip link show "$WAN_IF" | grep ether
ip -4 addr show "$WAN_IF" | grep inet || { echo "!! 未拿到 IP，回退：uci set network.wan2.disabled=1; ifup wan2"; exit 1; }

echo "=== [2/4] 无感放行验证（走该网卡源 IP 直测）==="
CODE=$(curl -s -m 10 -o /dev/null -w '%{http_code}' --interface "$WAN_IF" "$PROBE_URL")
echo "放行码: $CODE"
if [ "$CODE" != "204" ]; then
    echo "!! 非 204（$CODE），说明白名单未命中 / 真机未离线 / 触发互斥。"
    echo "   回退：uci set network.wan2.disabled=1; ifup wan2 后排查（见 setup.md §0）。"
    exit 1
fi
echo "   桶2 无感放行 OK"

echo "=== [3/4] 启动桶2 SOCKS 出口 ==="
IP_B=$(ip -4 addr show "$WAN_IF" | grep -o 'inet [0-9.]*' | head -1 | awk '{print $2}')
echo "桶2 源地址: $IP_B"
pkill -f "$SOCKSBIN" 2>/dev/null || true
sleep 1
nohup "$SOCKSBIN" -p "$SOCKS_PORT" -i "$SOCKS_LISTEN" -b "$IP_B" -q >/dev/null 2>&1 &
sleep 2
ss -tlnp 2>/dev/null | grep "$SOCKS_PORT" || echo "（未检测到监听，请人工核对 microsocks 是否运行）"

echo "=== [4/4] （可选）加入 multipath 分桶池 + L4 哈希 ==="
# 仅当 OpenWrt 有多个 WAN 腿需要一起按流分担时才需要；单账号双桶只有一条桶2
# 腿时，可跳过本段（SOCKS 已用 -b 绑定桶2 源地址）。
if [ "$MULTIPATH" = "1" ]; then
    ip route replace default \
        nexthop via "$GW_IP" dev "$WAN_IF" weight 1 2>/dev/null || echo "提示：默认路由已存在，请人工核对"
    # L4 哈希（含端口）：避免同目标连接全挤一条腿。必须打开，否则聚合"配了不提速"。
    sysctl -w net.ipv4.fib_multipath_hash_policy=1
    echo "   L4 哈希策略已设为 1（持久化见下方提示）"
fi

echo "SECOND-BUCKET-DONE"
echo "宿主机侧：在 host_aggregate.yaml 里把桶2 出口指到宿主可达的 <OpenWrt管理IP>:<SOCKS端口-B>（见模板）。"

# ---------------------------------------------------------------------------
# 持久化提示（重启后失效，需自行固化）：
#   · 网络配置：uci 已 commit，重启后仍生效；
#   · SOCKS 进程：加入 /etc/rc.local（先取 IP 再绑定，写成 bash -c 形式）：
#       bash -c 'IP=$(ip -4 addr show <物理WAN接口> | grep -o "inet [0-9.]*" | head -1 | awk "{print \$2}"); \
#                nohup /usr/bin/microsocks -p <SOCKS端口-B> -i <SOCKS监听地址> -b "$IP" -q >/dev/null 2>&1 &'
#   · multipath 路由 / sysctl：写进 /etc/rc.local 与 /etc/sysctl.conf；
#   · 保活：用 cron 每数分钟对数据面活的接口发少量维持包（只 ping 不重拨），
#     参考仓库根 templates/keepalive.service 与 docs/04。
# ---------------------------------------------------------------------------
