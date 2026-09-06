#!/bin/sh
# =============================================================================
# 模板：OpenWrt 多会话 + Multipath 出口配置脚本（脱敏模板）
#
# 【用途】
#   在一台 OpenWrt 路由器上，把"单条物理上行 + 多个认证会话"组织为多条出口
#   路径，实现多会话带宽合并：
#     步骤 1  用 macvlan 克隆多个 WAN 接口（每个 MAC 对应一个上游会话）；可选
#             mwan3 或纯内核 multipath 做负载均衡（推荐后者，见步骤 2）；
#     步骤 2  启动两个 SOCKS5 实例（分桶出口），推荐用"内核 multipath 默认路由
#             + L4 哈希"让各会话按流自动分担；另附曾用的 connmark + ip rule
#             策略路由方案，该方案已在实测中证伪（见文件内警告），仅保留供
#             对照，不建议直接使用；
#     步骤 3  打印接口 / 路由 / mwan3 / 哈希策略状态，供核对。
#   用法：
#     sh multipath_setup.sh                     # 默认执行全部步骤
#     RUN_STEP=1 sh multipath_setup.sh          # 只执行步骤 1（为 2/3 留出自由）
#
# 【前置条件】
#   · OpenWrt（含 opkg 包装的 uci / ip / iptables / mwan3），需要 root 执行。
#   · 已安装 mwan3：opkg update && opkg install mwan3（步骤 1 会前置检查）。
#   · 已安装 SOCKS 服务程序：本模板按 microsocks 的 -p/-i/-q 参数编写，改用
#     其它程序时请同步修改 SOCKSBIN 与启动参数。
#   · 上游对每一个 <MAC_A>..<MAC_C> 均有独立放行的会话（白名单 / 免密代认证），
#     且各会话互不干扰；本脚本不完成任何认证动作。
#   · 先把下方"占位符替换区"全部替换为你的环境值再运行。
#
# 【占位符清单】
#   <物理WAN接口>   物理上行在 OpenWrt 中的接口名，wan1 直接使用
#   <LAN接口>       内网桥接口（一般填 br-lan）
#   <MAC_A>          会话 A 的 MAC —— wan2（macvlan 克隆）
#   <MAC_B>          会话 B 的 MAC —— wan3
#   <MAC_C>          会话 C 的 MAC —— wan4
#   <探活目标IP>     联通性探测目标（建议用公共 DNS 地址）
#   <网段CIDR>       上游共享直连网段（含前缀长度的完整写法）
#   <网关IP>         上游网关地址
#   <内网IP_A>        会话 A 桶在本机的源地址（用于路由表 101）
#   <内网IP_B>        会话 B 桶在本机的源地址（用于路由表 102）
#   <出口接口A>      会话 A 桶的出口接口（物理口或 macvlan 设备名）
#   <出口接口B>      会话 B 桶的出口接口
#   <SOCKS端口-A>    桶 A 的 SOCKS 监听端口（如 1080）
#   <SOCKS端口-B>    桶 B 的 SOCKS 监听端口（如 1081）
#   <SOCKS监听地址>  SOCKS 监听地址（一般用通配监听地址）
#   <默认路由网段>    mwan3 默认路由规则的目标网段（一般用默认全零网段）
#
# 【风险提示】
#   · 仅限在自有设备 / 自有账号（或已获授权账号）上实验；遵守所在网络的接入
#     与使用规定，留意账号并发会话上限。
#   · 多 MAC 克隆 / 多会话会改变"设备数"表象，可能违反接入协议条款，后果自负。
#   · 步骤 1（若走 mwan3）会覆盖 /etc/config/mwan3；2A 段会替换默认路由，请
#     确认没有其它服务依赖原默认路由。
#   · mwan3 与手工 multipath 二选一，二者叠加会互相抢路由/规则表，务必核对
#     "哪条默认路由归谁管"，避免冲突造成丢包或环路。
#   · 步骤 2 的结果为内存态，重启后失效，需按步骤 2 末尾提示持久化。
#   · 本模板不含任何真实标识、凭据与路径；占位符规范见 docs/00_脱敏与贡献规范.md。
# =============================================================================
set -e

# ------------------------------ 占位符替换区 --------------------------------
WAN_PHY_IF="<物理WAN接口>"
LAN_IF="<LAN接口>"
MAC_A="<MAC_A>"
MAC_B="<MAC_B>"
MAC_C="<MAC_C>"
TRACK_IP="<探活目标IP>"
UP_NET="<网段CIDR>"
GW_IP="<网关IP>"
IP_A="<内网IP_A>"
IP_B="<内网IP_B>"
IF_A="<出口接口A>"
IF_B="<出口接口B>"
PORT_A="<SOCKS端口-A>"
PORT_B="<SOCKS端口-B>"
LISTEN_ADDR="<SOCKS监听地址>"
DEFAULT_NET="<默认路由网段>"
SOCKSBIN="/usr/bin/microsocks"   # SOCKS 服务程序路径（按需替换）
RUN_STEP="${RUN_STEP:-all}"      # all | 1 | 2 | 3
# ----------------------------------------------------------------------------

# =============================================================================
# 步骤 1：macvlan 多 WAN（+ 可选 mwan3 负载均衡）
# =============================================================================
step1() {
    echo "=== [1/3] macvlan 多 WAN（+ 可选 mwan3）==="
    # ---------------------------------------------------------------------
    # 说明（实测经验）：
    #   · macvlan 多接口划分会话是聚合的基础，本步的 macvlan / network 段是
    #     必做部分；
    #   · mwan3 负载均衡为"可选"：实测中发现 mwan3 在部分版本上存在 restart
    #     半途失败 / tracking 状态异常等行为差异，若使用异常，可改为纯内核
    #     multipath（步骤 2 的 2A 段），二者二选一，不要同时启用；
    #   · 无论走哪条路径，都必须在步骤 2 结尾把 L4 哈希策略打开，否则同
    #     目标（同 CDN IP）的连接会全部命中同一条腿（见步骤 2 警告）。
    # ---------------------------------------------------------------------
    [ -x /etc/init.d/mwan3 ] || {
        echo "未检测到 mwan3（可选）：跳过本步 mwan3 配置，转用纯内核 multipath（步骤 2 的 2A 段）即可。"
        echo "如需安装：opkg update && opkg install mwan3"
        return 0
    }

    # 1.1 创建 macvlan 设备与接口：
    #     wan2 = 克隆会话 A 的 MAC（若上游对"首会话"免密放行，此步保持上网不中断）
    #     wan3 / wan4 = 会话 B / C 的 MAC
    uci set network.wan2_dev=device
    uci set network.wan2_dev.name='wan2'
    uci set network.wan2_dev.type='macvlan'
    uci set network.wan2_dev.ifname="$WAN_PHY_IF"
    uci set network.wan2_dev.mode='bridge'
    uci set network.wan2=interface
    uci set network.wan2.device='wan2'
    uci set network.wan2.proto='dhcp'
    uci set network.wan2.macaddr="$MAC_A"   # 新版 OpenWrt 更推荐放到 device 段的 macaddr

    n=3
    for MAC in "$MAC_B" "$MAC_C"; do
        uci set network.wan${n}_dev=device
        uci set network.wan${n}_dev.name="wan${n}"
        uci set network.wan${n}_dev.type='macvlan'
        uci set network.wan${n}_dev.ifname="$WAN_PHY_IF"
        uci set network.wan${n}_dev.mode='bridge'
        uci set network.wan${n}=interface
        uci set network.wan${n}.device="wan${n}"
        uci set network.wan${n}.proto='dhcp'
        uci set network.wan${n}.macaddr="$MAC"
        n=$((n+1))
    done
    # 如需更多会话：新增 <MAC_x> 变量，并在此循环里多放一个值（wan5、wan6…）
    uci commit network
    /etc/init.d/network reload
    sleep 3

    # 1.2 生成 mwan3 配置（覆盖旧文件）：wan1=物理口，wan2..wan4=macvlan 设备
    : > /etc/config/mwan3
    cat >> /etc/config/mwan3 <<EOF
config globals 'globals'
	option enabled '1'

EOF
    n=1
    while [ "$n" -le 4 ]; do
        if [ "$n" = 1 ]; then dev="$WAN_PHY_IF"; else dev="wan${n}"; fi
        cat >> /etc/config/mwan3 <<EOF
config interface 'wan${n}'
	option enabled '1'
	option device '$dev'
	option track_ip '$TRACK_IP'
	option check_interval '5'
	option reliability '1'

config member 'wan${n}_m1'
	option interface 'wan${n}'
	option metric '1'
	option weight '1'

EOF
        n=$((n+1))
    done
    cat >> /etc/config/mwan3 <<EOF
config policy 'loadbalance'
	option last_resort 'unreachable'
EOF
    n=1
    while [ "$n" -le 4 ]; do
        echo "	list use_member 'wan${n}_m1'" >> /etc/config/mwan3
        n=$((n+1))
    done
    cat >> /etc/config/mwan3 <<EOF

config rule 'default_route'
	option dest_ip '$DEFAULT_NET'
	option proto 'all'
	option family 'ipv4'
	option use_policy 'loadbalance'
EOF
    /etc/init.d/mwan3 restart
    sleep 3
}

# =============================================================================
# 步骤 2：双 SOCKS 分桶出口
# =============================================================================
step2() {
    echo "=== [2/3] 双 SOCKS 分桶出口（推荐 2A：纯内核 multipath；2B 为已证伪方案，仅对照）==="

    # ---------------------------------------------------------------------
    # 【推荐方案 2A：内核 multipath 默认路由 + L4 哈希】（实测有效）
    #
    #   原理：不按"入口目的端口"人为分桶，而是让内核按流的 L4 五元组哈希，
    #   把每个新建连接稳定分发到某一条 WAN 腿。同一连接恒走同一腿（TCP 不
    #   乱序、NAT 绑定不破坏），多条并发连接按哈希统计性摊到各腿。
    #
    #   为什么不用 mwan3（实测结论）：mwan3 本质也是多路由分担，但在部分
    #   版本上 restart 半途失败 / tracking 状态异常，且 mwan3 弃用后可去掉
    #   一层用户态依赖；纯内核 multipath 更轻量、行为更可预期。
    #
    #   ⚠️ 关键坑（实测踩过）：Linux 默认 fib_multipath_hash_policy=0（L3 哈希，
    #   只按源/目的 IP），同 CDN 域名解析出的少数 IP 会让大量连接全部命中
    #   同一条腿 → 一腿打满、其余腿空转。必须打开 L4 哈希（=1，含端口），
    #   见下方【L4 哈希策略】段。
    # ---------------------------------------------------------------------

    # 2A.1 停旧实例，再按桶 A / 桶 B 各起一个 SOCKS 服务
    #      （两个实例监听不同端口；连接由内核按 L4 哈希分到各 WAN 腿）
    pkill -f "$SOCKSBIN" 2>/dev/null || true
    sleep 1
    nohup "$SOCKSBIN" -p "$PORT_A" -i "$LISTEN_ADDR" -q >/dev/null 2>&1 &
    nohup "$SOCKSBIN" -p "$PORT_B" -i "$LISTEN_ADDR" -q >/dev/null 2>&1 &
    sleep 2
    ss -tlnp 2>/dev/null | grep -E "$PORT_A|$PORT_B" || true

    # 2A.2 内核 multipath 默认路由（两腿各 weight 1；多腿可继续追加 nexthop）
    #       注意：macvlan 子接口（wan2/wan3）是 dhcp 拿 IP，先确认它们有地址
    #       再配路由；接口名按你的实际环境替换（<出口接口A>/<出口接口B>）
    ip route replace default \
        nexthop via "$GW_IP" dev "$IF_A" weight 1 \
        nexthop via "$GW_IP" dev "$IF_B" weight 1 2>/dev/null || echo "   提示：默认路由已存在或接口未就绪，请人工核对"

    # 【L4 哈希策略】（实测必需，否则同目标连接全挤在一腿）
    sysctl -w net.ipv4.fib_multipath_hash_policy=1
    # 持久化（写进 /etc/sysctl.conf 等效配置，重启后仍生效）：
    #   echo 'net.ipv4.fib_multipath_hash_policy=1' >> /etc/sysctl.conf

    # 2A.3 若同时启用了 mwan3，需将其默认策略停掉，避免与 multipath 冲突
    if [ -x /usr/sbin/mwan3 ]; then
        /etc/init.d/mwan3 stop 2>/dev/null || true
        echo "   提示：已停止 mwan3（与纯内核 multipath 二选一，避免冲突）"
    fi

    # ---------------------------------------------------------------------
    # 【方案 2B：connmark + fwmark 策略路由】—— 已实测证伪，仅保留作对照
    #
    #   曾用思路：对进入本机、目的端口为桶 A/B 的连接打 connmark，再把
    #   OUTPUT 上的包按 connmark 打 fwmark，用 ip rule 把标记包送进独立
    #   路由表（每桶一条），实现"按入口端口确定性分桶"。
    #
    #   实测证伪原因（两条致命缺陷）：
    #     1. 入站连接上的 CONNMARK 不会传递给本机 SOCKS 服务（如 microsocks）
    #        新建的出站连接 —— fwmark 规则对真正往上游的流量根本不生效；
    #     2. 更糟：入站连接本身在 OUTPUT 上被 -m connmark 命中并打 mark，
    #        其 reply 包被策略路由送进 WAN 表 → 回不到 br-lan → SOCKS 黑洞。
    #     实测表现为：启用后 SOCKS 端口立刻全断。
    #
    #   结论：除非给 SOCKS 服务打 SO_BINDTODEVICE 补丁（或每个桶一套独立
    #   network namespace），否则不要用本方案做确定性分桶；统计性分桶用 2A
    #   足够（多连接应用天然吃满各腿）。
    # ---------------------------------------------------------------------
    # 2B（已废弃，勿启用）：
    # iptables -t mangle -F
    # ip rule del fwmark 1 table 101 2>/dev/null || true
    # ip rule del fwmark 2 table 102 2>/dev/null || true
    # ip route flush table 101 2>/dev/null || true
    # ip route flush table 102 2>/dev/null || true
    # iptables -t mangle -A PREROUTING -i "$LAN_IF" -p tcp --dport "$PORT_A" -j CONNMARK --set-mark 1
    # iptables -t mangle -A PREROUTING -i "$LAN_IF" -p tcp --dport "$PORT_B" -j CONNMARK --set-mark 2
    # iptables -t mangle -A OUTPUT -m connmark --mark 1 -j MARK --set-mark 1
    # iptables -t mangle -A OUTPUT -m connmark --mark 2 -j MARK --set-mark 2
    # ip route add "$UP_NET" dev "$IF_A" src "$IP_A" table 101
    # ip route add default via "$GW_IP" dev "$IF_A" table 101
    # ip route add "$UP_NET" dev "$IF_B" src "$IP_B" table 102
    # ip route add default via "$GW_IP" dev "$IF_B" table 102
    # ip rule add fwmark 1 table 101 priority 100
    # ip rule add fwmark 2 table 102 priority 200

    echo "   提示：multipath 路由与 sysctl 为内存态，重启后失效；请按上方注释持久化。"
}

# =============================================================================
# 步骤 3：状态核对
# =============================================================================
step3() {
    echo "=== [3/3] 状态核对 ==="
    echo "--- 接口 ---"
    ip -4 addr show | grep -E '^[0-9]+:|inet ' | grep -v '127\.' || true
    echo "--- 默认路由 ---"
    ip route show default
    echo "--- L4 哈希策略（应为 1，否则同目标连接会全挤一腿）---"
    sysctl net.ipv4.fib_multipath_hash_policy 2>/dev/null || echo "（当前内核不支持或未开启）"
    echo "--- 策略路由 ---"
    ip rule show
    ip route show table 101 2>/dev/null || true
    ip route show table 102 2>/dev/null || true
    echo "--- mwan3 ---"
    if [ -x /usr/sbin/mwan3 ]; then
        mwan3 status 2>&1 | head -40 || true
    else
        echo "mwan3 命令不存在（不影响其它步骤）"
    fi
    echo "MULTIPATH-SETUP-DONE"
}

case "$RUN_STEP" in
    all) step1; step2; step3 ;;
    1)   step1 ;;
    2)   step2 ;;
    3)   step3 ;;
    *)   echo "RUN_STEP 取值：all | 1 | 2 | 3"; exit 1 ;;
esac