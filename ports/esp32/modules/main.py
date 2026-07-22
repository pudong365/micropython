import machine
from machine import Pin, ADC, SoftI2C, UART
import network
import socket
import time
import espnow
import ubinascii

# ==================== 通信模式选择 ====================
COMM_MODE = 0  # 1=ESP-NOW, 2=WiFi STA→B的AP, 3=LoRa(LR41-24T12S), 4=WiFi STA(路由器YTS) 0=选择方式
# ==================================================

# ==================== CONFIG ====================
A_PINS = [7, 8, 9, 10]
PORT = 8080
BASELINE_SAMPLES = 20

# ==================== 2-WIRE RELAY TEST PINS ====================
RELAY_PIN = 13
DETECT_PIN = 48
GND_PIN = 7

# ==================== ADG2128 PINS ====================
ADG_SCL = 1
ADG_SDA = 2
ADG_RST = 4

# ==================== WiFi配置 (COMM_MODE=2, B的AP) ====================
B_AP_SSID = "ESP32_B"
B_IP = "192.168.4.1"

# ==================== WiFi配置 (COMM_MODE=4, 路由器YTS) ====================
ROUTER_SSID = "YTS"
ROUTER_PASS = "Zjd246400.."

# ==================== LoRa配置 (DX-LR41-24T12S, 2.4GHz) ====================
LORA_TX = 17       # ESP32 TX → LR41 RX
LORA_RX = 18       # ESP32 RX ← LR41 TX
LORA_M0 = 21       # 引脚控制(AT+SWITCH=0时默认输出脚)
LORA_M1 = 20       # 引脚控制
LORA_AUX = 19      # AUX状态指示(高=忙, 低=空闲)
LORA_BAUD = 9600   # 串口波特率(默认9600/8/N/1)
LORA_ADDR = 0xFFFF # 模块地址(默认FFFF, 透明传输不需要)
LORA_CHANNEL = 0x0C # 信道: 0C=2424MHz(默认)
LORA_LEVEL = 3     # 速率等级(默认3=10kbps, 2.2km)
LORA_POWER = 13    # 发射功率dBm(默认13dBm)
# ==================================================

# 全局变量
B_MAC = None       # ESP-NOW模式使用
e = None
my_ip = None
MY_IP = None       # 本机IP (用于自广播过滤)
sta = None
lora_uart = None
udp_recv_sock = None
B_IP_ADDR = None       # 路由器模式下B的IP (从接收消息捕获)


def show_info(text, color=None):
    print("A: {}".format(text))


# ==================== 统一通信初始化 ====================
def comm_init():
    global e, sta, lora_uart, B_MAC, my_ip, MY_IP, udp_recv_sock

    if COMM_MODE == 1:
        # ---- ESP-NOW模式 ----
        show_info("ESP-NOW初始化...")
        # WiFi预热: 先激活AP再关(解决PSRAM/NVS不稳定导致STA无法初始化)
        wlan = network.WLAN(network.AP_IF)
        try:
            wlan.active(True)
            wlan.active(False)
        except:
            pass
        time.sleep_ms(100)
        sta = network.WLAN(network.STA_IF)
        for retry in range(5):
            try:
                sta.active(True)
                break
            except Exception as ex:
                show_info("WiFi重试 {}/5".format(retry+1))
                print("WiFi激活失败(重试 {}/5): {}".format(retry+1, ex))
                time.sleep_ms(500)
                if retry == 4:
                    import machine
                    machine.reset()
        e = espnow.ESPNow()
        e.active(True)
        show_info("ESP-NOW OK")
        return sta

    elif COMM_MODE == 2:
        # ---- WiFi STA模式 ----
        show_info("WiFi连接中...")
        sta = network.WLAN(network.STA_IF)
        sta.active(True)
        try:
            sta.config(pm=sta.PM_NONE)
        except:
            pass
        try:
            sta.config(txpower=20)
        except:
            pass
        while not sta.isconnected():
            sta.disconnect()
            time.sleep_ms(100)
            sta.connect(B_AP_SSID, "")
            for i in range(30):
                time.sleep_ms(500)
                if sta.isconnected():
                    break
            if not sta.isconnected():
                sta.active(False)
                time.sleep_ms(500)
                sta.active(True)
        my_ip = sta.ifconfig()[0]
        MY_IP = my_ip
        udp_recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_recv_sock.bind(('0.0.0.0', PORT))
        udp_recv_sock.setblocking(False)
        show_info("WiFi OK: {}".format(my_ip))
        return sta

    elif COMM_MODE == 3:
        # ---- LoRa模式 (DX-LR41-24T12S, 2.4GHz) ----
        show_info("LoRa初始化 (LR41-24T12S)...")
        pin_m0 = Pin(LORA_M0, Pin.OUT, value=0)
        pin_m1 = Pin(LORA_M1, Pin.OUT, value=0)
        time.sleep_ms(100)

        # LR41默认透明传输模式(AT+MODE0), 默认信道0C=2424MHz
        # 只需初始化UART即可收发
        u = UART(1, baudrate=LORA_BAUD, tx=Pin(LORA_TX), rx=Pin(LORA_RX))
        time.sleep_ms(100)

        lora_uart = u
        show_info("LR41 OK (2.4GHz, ch={})".format(LORA_CHANNEL))
        return None

    elif COMM_MODE == 4:
        # ---- WiFi STA模式 (连接路由器YTS) ----
        show_info("路由器WiFi连接中...")
        sta = network.WLAN(network.STA_IF)
        sta.active(True)
        time.sleep_ms(100)
        sta.disconnect()
        time.sleep_ms(100)
        try:
            sta.config(pm=sta.PM_NONE)
        except:
            pass
        try:
            sta.config(txpower=20)
        except:
            pass
        hard_reset = 0
        while not sta.isconnected():
            sta.disconnect()
            time.sleep_ms(100)
            sta.connect(ROUTER_SSID, ROUTER_PASS)
            for i in range(30):
                time.sleep_ms(500)
                if sta.isconnected():
                    break
            if not sta.isconnected():
                hard_reset += 1
                if hard_reset >= 3:
                    sta.active(False)
                    time.sleep_ms(500)
                    sta.active(True)
                    hard_reset = 0
        my_ip = sta.ifconfig()[0]
        MY_IP = my_ip
        udp_recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_recv_sock.bind(('0.0.0.0', PORT))
        udp_recv_sock.setblocking(False)
        show_info("WiFi OK: {}".format(my_ip))
        return sta


# ==================== 统一发送/接收 ====================
def send_udp(msg, target=None):
    """通用发送函数 (兼容所有模式)"""
    global B_MAC, e, lora_uart
    if COMM_MODE == 1:
        # ESP-NOW
        if target is None:
            target = B_MAC
        if target is None:
            print("  ESP-NOW发送失败: B_MAC未设置")
            return False
        try:
            e.send(target, msg.encode())
            return True
        except:
            return False
    elif COMM_MODE == 2:
        # WiFi AP模式: A广播到255.255.255.255 (绕过ESP32 AP接口UDP交付问题)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(msg.encode(), ('255.255.255.255', PORT))
            s.close()
            return True
        except:
            return False
    elif COMM_MODE == 4:
        # WiFi路由器模式: 直接发送到B的IP
        if target is None:
            target = B_IP_ADDR
        if target is None:
            return False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(msg.encode(), (target, PORT))
            s.close()
            return True
        except:
            return False
    elif COMM_MODE == 3:
        # LoRa UART
        try:
            lora_uart.write((msg + "\n").encode())
            return True
        except:
            return False
    return False


def wait_response(timeout_ms=2000):
    """通用接收函数 (等一条回复)"""
    global B_MAC, e, lora_uart, udp_recv_sock, B_IP_ADDR

    if COMM_MODE == 1:
        # ESP-NOW
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            try:
                mac, msg = e.recv(0)
                if msg:
                    response = msg.decode().strip()
                    if response:
                        return response
            except:
                pass
            time.sleep_ms(50)
        return None

    elif COMM_MODE == 2 or COMM_MODE == 4:
        # WiFi UDP (模式2=连B的AP, 模式4=路由器YTS)
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            try:
                msg, addr = udp_recv_sock.recvfrom(64)
                response = msg.decode().strip()
                if response:
                    # 过滤自广播 (模式2广播会回环)
                    if MY_IP and addr[0] == MY_IP:
                        continue
                    if COMM_MODE == 4 and B_IP_ADDR is None:
                        B_IP_ADDR = addr[0]
                    return response
            except:
                pass
            time.sleep_ms(50)
        return None

    elif COMM_MODE == 3:
        # LoRa UART
        start = time.ticks_ms()
        buf = b""
        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            if lora_uart.any():
                data = lora_uart.read()
                if data:
                    buf += data
                    # 检查是否有完整行
                    if b'\n' in buf:
                        line, _, rest = buf.partition(b'\n')
                        return line.decode().strip()
            time.sleep_ms(20)
        if buf:
            return buf.decode().strip()
        return None

    return None


def wait_for_a_response(timeout_ms=5000):
    return wait_response(timeout_ms)


def broadcast_udp(msg, burst=3):
    """广播发送(仅COMM_MODE=4发现阶段使用,路由器模式下广播A_READY)"""
    try:
        for i in range(burst):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(msg.encode(), ('255.255.255.255', PORT))
            s.close()
            time.sleep_ms(10)
        return True
    except:
        return False


def flush_echo():
    """清空LoRa自回显(LR41半双工会收到自己发送的echo)"""
    global lora_uart
    if COMM_MODE == 3:
        time.sleep_ms(30)
        try:
            while lora_uart.any():
                lora_uart.read()
        except:
            pass


# ==================== ESP-NOW 发现流程 ====================
def discover_b_board():
    """等待B板发现请求并响应 (仅ESP-NOW模式)"""
    global B_MAC, e
    if COMM_MODE != 1:
        return True  # 其他模式不需要发现

    print("等待B板发现...")
    show_info("Waiting B...")
    broadcast_mac = b'\xff\xff\xff\xff\xff\xff'
    e.add_peer(broadcast_mac)
    wlan = network.WLAN(network.STA_IF)
    a_mac_str = ubinascii.hexlify(wlan.config('mac')).decode().upper()
    start = time.ticks_ms()
    timeout = 30000
    while time.ticks_diff(time.ticks_ms(), start) < timeout:
        try:
            mac, msg = e.recv(0)
            if msg:
                cmd = msg.decode().strip()
                if cmd == "ESP_NOW_DISCOVER":
                    response = "ESP_NOW_ACK:" + a_mac_str
                    e.add_peer(mac)
                    e.send(mac, response.encode())
                    B_MAC = mac
                    print("  发现B板 MAC: {}".format(ubinascii.hexlify(B_MAC).decode().upper()))
                    show_info("B Found!")
                    return True
        except:
            pass
        time.sleep_ms(50)
    print("  超时: 未收到B板发现请求")
    show_info("B Not Found")
    return False


# ========== ADC INIT ==========
adcs = {}
for pin in A_PINS:
    adcs[pin] = ADC(Pin(pin, Pin.IN))
    adcs[pin].atten(ADC.ATTN_11DB)


def measure_signal(pin):
    samples = []
    for i in range(10):
        val = adcs[pin].read()
        if val > 4095:
            val = 4095
        if val < 0:
            val = 0
        samples.append(val)
        time.sleep_ms(3)
    if not samples:
        return 0
    avg = sum(samples) // len(samples)
    ac = sum(abs(s - avg) for s in samples) // len(samples)
    if ac > 4095:
        ac = 4095
    return ac


def get_baseline():
    show_info("Calibrating...")
    baselines = {}
    for pin in A_PINS:
        total = 0
        for i in range(BASELINE_SAMPLES):
            total += measure_signal(pin)
            time.sleep_ms(50)
        baselines[pin] = total // BASELINE_SAMPLES
    show_info("Calibration OK")
    return baselines


# ========== MODE 1: SOFTWARE RADIATION ==========
def software_mode():
    global e
    show_info("Software Mode")
    baselines = get_baseline()
    for pin, base in baselines.items():
        show_info("P{}:{}".format(pin, base))
    Pin(RELAY_PIN, Pin.OUT).value(0)
    print("A: Software mode started")
    while True:
        try:
            if COMM_MODE == 1:
                mac, raw = e.recv(0)
                if raw:
                    cmd = raw.decode().strip()
                else:
                    cmd = None
            else:
                cmd = wait_response(500)
            if cmd:
                print("A: Received: {}".format(cmd))
                if cmd.startswith("SCAN:"):
                    b_pin = int(cmd.split(":")[1])
                    print("A: SCAN from B{}".format(b_pin))
                    results = {}
                    for pin in A_PINS:
                        results[pin] = measure_signal(pin)
                    effective = {}
                    for pin in A_PINS:
                        val = results[pin] - baselines[pin]
                        effective[pin] = val if val > 0 else 0
                    best_pin = max(effective, key=effective.get)
                    parts = ["{}:{}".format(pin, results[pin]) for pin in A_PINS]
                    send_udp("PINS:" + ",".join(parts))
                    print("  Best: P{} strength={}".format(best_pin, effective[best_pin]))
                    show_info("B{} -> P{}".format(b_pin, best_pin))
                elif cmd == "BASELINE_REQ":
                    parts = ["{}:{}".format(pin, baselines[pin]) for pin in A_PINS]
                    send_udp("BASELINE:" + ",".join(parts))
                    print("A: Sent baseline")
                    show_info("Baseline sent")
        except:
            pass
        time.sleep_ms(30)


# ========== MODE 2: 2-WIRE RELAY TEST ==========
def hardware_2wires_mode():
    global e
    show_info("2-Wire Mode")
    relay = Pin(RELAY_PIN, Pin.OUT)
    relay.value(0)
    Pin(DETECT_PIN, Pin.IN)
    # Pin7 GND检查 (与独立版一致)
    gnd_pin = Pin(GND_PIN, Pin.IN)
    if gnd_pin.value() == 0:
        show_info("Pin7 GND OK")
    else:
        show_info("Pin7 GND FAIL")
    print("A: 2-Wire relay mode started")

    relay_on = False
    relay_locked = False
    while True:
        try:
            if COMM_MODE == 1:
                mac, raw = e.recv(0)
                if raw:
                    cmd = raw.decode().strip()
                else:
                    cmd = None
            else:
                cmd = wait_response(500)
            if cmd:
                print("A: Received: {}".format(cmd))
                if cmd == "RELAY_ON":
                    relay.value(1)
                    relay_on = True
                    relay_locked = True
                    show_info("Relay ON")
                    time.sleep_ms(300)
                    for i in range(5):
                        send_udp("RELAY_READY")
                        time.sleep_ms(50)
                    print("A: Relay ON (locked) -> RELAY_READY x5")
                elif cmd == "RELAY_OFF":
                    if not relay_locked:
                        relay.value(0)
                        relay_on = False
                        print("A: Relay OFF")
                        show_info("Relay OFF")
                        if COMM_MODE == 3:
                            time.sleep_ms(200)
                            send_udp("RELAY_OFF_DONE")
                    else:
                        print("A: Relay OFF ignored (locked)")
                        show_info("Relay LOCKED")
                elif cmd == "RELAY_UNLOCK":
                    relay_locked = False
                    relay.value(0)
                    relay_on = False
                    print("A: Relay UNLOCKED and OFF")
                    show_info("Relay OFF")
                    if COMM_MODE == 3:
                        time.sleep_ms(200)
                        send_udp("RELAY_UNLOCK_DONE")
                elif cmd == "READ_PIN1":
                    level = Pin(DETECT_PIN, Pin.IN).value()
                    resp = "PIN{}=1".format(DETECT_PIN) if level else "PIN{}=0".format(DETECT_PIN)
                    send_udp(resp)
                    print("A: {} (DETECT_PIN=Pin{})".format(resp, DETECT_PIN))
                elif cmd == "READ_PIN7":
                    level = Pin(7, Pin.IN).value()
                    send_udp("PIN7={}".format(level))
                elif cmd == "READ_PIN8":
                    level = Pin(8, Pin.IN).value()
                    send_udp("PIN8={}".format(level))
                elif cmd == "READ_PIN9":
                    level = Pin(9, Pin.IN).value()
                    send_udp("PIN9={}".format(level))
                elif cmd == "READ_PIN10":
                    level = Pin(10, Pin.IN).value()
                    send_udp("PIN10={}".format(level))
                elif cmd == "MODE:HARDWARE_2WIRES":
                    send_udp("ADG_MODE_READY")
        except:
            pass
        time.sleep_ms(30)


# ========== MODE 3: ADG2128 MULTI-WIRE ==========
def hardware_multiwires_mode():
    global e
    show_info("ADG2128 Pairing Mode")
    Pin(RELAY_PIN, Pin.OUT).value(0)

    adg = None
    try:
        from adg2128 import ADG2128
        Pin(ADG_SCL, Pin.IN)
        Pin(ADG_SDA, Pin.IN)
        time.sleep_ms(10)
        i2c = machine.I2C(0, scl=Pin(ADG_SCL), sda=Pin(ADG_SDA), freq=100000)
        adg = ADG2128(i2c, reset_pin=ADG_RST)
        show_info("ADG2128 OK")
        print("A: ADG2128 initialized")
        send_udp("ADG_MODE_READY")
    except Exception as e:
        print("A: ADG2128 init failed: {}".format(e))
        show_info("ADG2128 FAIL")
        send_udp("ADG_MODE_FAIL")

    show_info("Waiting B pairing cmds...")
    while True:
        try:
            if COMM_MODE == 1:
                mac, raw = e.recv(0)
                if raw:
                    cmd = raw.decode().strip()
                else:
                    cmd = None
            else:
                cmd = wait_response(500)
            if cmd:
                print("A: Received: {}".format(cmd))
                if cmd == "MODE:ADG_MULTI":
                    if COMM_MODE == 3:
                        time.sleep_ms(200)  # LoRa半双工: B需要TX→RX切换时间
                    send_udp("ADG_MODE_READY" if adg is not None else "ADG_MODE_FAIL")
                    continue
                if adg is None:
                    send_udp("ADG_ERROR:not_initialized")
                    continue
                if cmd.startswith("ON:"):
                    parts = cmd.split(":")
                    y1 = int(parts[1])
                    y2 = int(parts[2])
                    ok1 = ok2 = False
                    try:
                        ok1 = adg.on(0, y1)
                    except:
                        pass
                    try:
                        ok2 = adg.on(0, y2)
                    except:
                        pass
                    if ok1 and ok2:
                        if COMM_MODE == 3:
                            time.sleep_ms(200)
                        send_udp("ON_OK:{}:{}".format(y1, y2))
                    else:
                        send_udp("ON_FAIL:{}:{}".format(y1, y2))
                elif cmd == "OFF":
                    try:
                        adg.all_off()
                        if COMM_MODE == 3:
                            time.sleep_ms(200)
                        send_udp("OFF_DONE")
                    except:
                        send_udp("OFF_FAIL")
                elif cmd == "READ_PIN1":
                    level = Pin(1, Pin.IN).value()
                    send_udp("PIN1={}".format(level))
                elif cmd == "READ_PIN7":
                    level = Pin(7, Pin.IN).value()
                    send_udp("PIN7={}".format(level))
                elif cmd == "READ_PIN8":
                    level = Pin(8, Pin.IN).value()
                    send_udp("PIN8={}".format(level))
                elif cmd == "READ_PIN9":
                    level = Pin(9, Pin.IN).value()
                    send_udp("PIN9={}".format(level))
                elif cmd == "READ_PIN10":
                    level = Pin(10, Pin.IN).value()
                    send_udp("PIN10={}".format(level))
        except:
            pass
        time.sleep_ms(30)


def auto_scan_mode():
    """自动扫描: WiFi→ESP-NOW→Router→LoRa, 每模式60s. 返回选定的COMM_MODE."""
    global B_MAC
    for m in [3, 4, 2, 1]:
        show_info("Auto: try mode {}".format(m))
        if m == 2:
            s = network.WLAN(network.STA_IF)
            s.active(True)
            try: s.config(pm=s.PM_NONE)
            except: pass
            try: s.config(txpower=20)
            except: pass
            t0 = time.ticks_ms()
            ok = False
            while time.ticks_diff(time.ticks_ms(), t0) < 60000:
                if not s.isconnected():
                    s.disconnect(); time.sleep_ms(100)
                    s.connect(B_AP_SSID, "")
                for i in range(20):
                    time.sleep_ms(500)
                    if s.isconnected(): ok = True; break
                if ok: break
                s.active(False); time.sleep_ms(500); s.active(True)
            if ok:
                show_info("Auto: WiFi OK")
                return 2
            s.active(False)
        elif m == 1:
            try:
                wlan = network.WLAN(network.AP_IF)
                try: wlan.active(True); wlan.active(False)
                except: pass
                time.sleep_ms(100)
                wlan = network.WLAN(network.STA_IF)
                wlan.active(True)
                ee = espnow.ESPNow(); ee.active(True)
                a_mac_str = ubinascii.hexlify(wlan.config('mac')).decode().upper()
                bc = b'\xff\xff\xff\xff\xff\xff'; ee.add_peer(bc)
                t0 = time.ticks_ms(); found = False
                while time.ticks_diff(time.ticks_ms(), t0) < 60000:
                    try:
                        mac, msg = ee.recv(0)
                        if msg and msg.decode().strip() == "ESP_NOW_DISCOVER":
                            ee.add_peer(mac)
                            ee.send(mac, ("ESP_NOW_ACK:" + a_mac_str).encode())
                            B_MAC = mac; found = True; break
                    except: pass
                    time.sleep_ms(50)
                ee.active(False)
                if found:
                    show_info("Auto: ESP-NOW OK")
                    return 1
                show_info("Auto: ESP-NOW fail")
            except Exception as ex:
                show_info("Auto: ESP-NOW err {}".format(ex))
        elif m == 4:
            s = network.WLAN(network.STA_IF)
            s.active(True); time.sleep_ms(100); s.disconnect(); time.sleep_ms(100)
            try: s.config(pm=s.PM_NONE)
            except: pass
            try: s.config(txpower=20)
            except: pass
            t0 = time.ticks_ms(); ok = False
            while time.ticks_diff(time.ticks_ms(), t0) < 60000:
                s.disconnect(); time.sleep_ms(100)
                s.connect(ROUTER_SSID, ROUTER_PASS)
                for i in range(30):
                    time.sleep_ms(500)
                    if s.isconnected(): ok = True; break
                if ok: break
                s.active(False); time.sleep_ms(500); s.active(True)
            if ok:
                show_info("Auto: Router OK")
                return 4
            s.active(False)
        elif m == 3:
            try:
                Pin(LORA_M0, Pin.OUT, value=0)
                Pin(LORA_M1, Pin.OUT, value=0)
                time.sleep_ms(100)
                UART(1, baudrate=LORA_BAUD, tx=Pin(LORA_TX), rx=Pin(LORA_RX))
                show_info("Auto: LoRa OK")
                return 3
            except Exception as ex:
                show_info("Auto: LoRa err {}".format(ex))
    return 3


def mode0_start():
    """COMM_MODE=0: 先以WiFi连接B → 发MY_IP → 等B发MODE:X → 返回选定模式.
    WiFi连不上则auto_scan_mode()."""
    show_info("Mode0: WiFi init...")

    # Phase 1: 连接B的WiFi AP (60s超时)
    s = network.WLAN(network.STA_IF)
    s.active(True)
    try: s.config(pm=s.PM_NONE)
    except: pass
    try: s.config(txpower=20)
    except: pass
    t0 = time.ticks_ms()
    connected = False
    while time.ticks_diff(time.ticks_ms(), t0) < 60000:
        if not s.isconnected():
            s.disconnect(); time.sleep_ms(100)
            s.connect(B_AP_SSID, "")
        for i in range(20):
            time.sleep_ms(500)
            if s.isconnected(): connected = True; break
        if connected: break
        if not s.isconnected():
            s.active(False); time.sleep_ms(500); s.active(True)

    if not connected:
        show_info("WiFi fail, auto-scan...")
        s.active(False)
        return auto_scan_mode()

    # Phase 2: WiFi已连接, 发IP给B, 等MODE命令
    my_ip = s.ifconfig()[0]
    show_info("WiFi OK: {}".format(my_ip))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', PORT))
    sock.setblocking(False)

    try:
        sock.sendto(("MY_IP:{}".format(my_ip)).encode(), (B_IP, PORT))
        show_info("Sent MY_IP to B")
    except Exception as ex:
        show_info("Send err: {}".format(ex))

    # Phase 3: 等B发MODE:X (60s超时)
    show_info("Wait B mode...")
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < 60000:
        try:
            msg, addr = sock.recvfrom(64)
            if msg:
                cmd = msg.decode().strip()
                if cmd.startswith("MODE:") and addr[0] != my_ip:
                    new_mode = int(cmd.split(":")[1])
                    show_info("B mode: {}".format(new_mode))
                    # 回复确认给B
                    # 连发5次ACK确保B收到 (UDP可能丢包)
                    for ack_i in range(5):
                        try:
                            sock.sendto(b"MODE_ACK", addr)
                            time.sleep_ms(20)
                        except Exception as ex:
                            print("  MODE_ACK发送失败: {}".format(ex))
                    print("  MODE_ACK已发送给B (5次)")
                    sock.close()
                    if new_mode != 2:
                        s.active(False)  # 非WiFi模式要断开
                    return new_mode
        except:
            pass
        time.sleep_ms(50)

    # 无命令 → 默认LoRa
    show_info("No cmd, default LoRa")
    sock.close()
    s.active(False)
    return 3


# ===================================================================
def main():
    global B_MAC, e, COMM_MODE

    show_info("A Board Start")
    time.sleep_ms(300)

    # COMM_MODE=0: 先WiFi连B, 等B发模式选择
    if COMM_MODE == 0:
        COMM_MODE = mode0_start()
        show_info("Mode0 selected: {}".format(COMM_MODE))

    comm_init()

    # 输出本机标识
    mode_names = {1: "ESP-NOW", 2: "WiFi", 3: "LoRa", 4: "Router"}
    show_info("Mode: {}".format(mode_names.get(COMM_MODE, "?")))

    if COMM_MODE == 2:
        # WiFi: 发送本机IP让B板知晓 (与独立版一致)
        my_ip = sta.ifconfig()[0]
        print("A: WiFi OK, IP={}".format(my_ip))
        show_info(my_ip)
        send_udp("MY_IP:{}".format(my_ip))
    elif COMM_MODE == 4:
        my_ip = sta.ifconfig()[0]
        print("A: RouterWiFi OK, IP={}".format(my_ip))
        show_info(my_ip)
        # 不发送MY_IP, 通过A_READY广播让B发现A
    elif COMM_MODE == 1:
        wlan = network.WLAN(network.STA_IF)
        a_mac_str = ubinascii.hexlify(wlan.config('mac')).decode().upper()
        print("A板 MAC: {}".format(a_mac_str))
        show_info("MAC: {}".format(a_mac_str[:8]))
        show_info("Waiting B...")
        b_found = discover_b_board()
        if b_found:
            show_info("B Found!")
        else:
            show_info("B Not Found!")

    print("A: Waiting for mode command...")
    mode = None
    last_ready_send = 0
    ready_interval = 2000

    # 主循环等命令
    while mode is None:
        now = time.ticks_ms()

        # 定期发A_READY通知B (ESP-NOW模式用广播, WiFi/LoRa用对应方式)
        if now - last_ready_send > ready_interval:
            if COMM_MODE == 1:
                if B_MAC is not None:
                    send_udp("A_READY")
                else:
                    try:
                        e.send(b'\xff\xff\xff\xff\xff\xff', b"A_READY")
                    except:
                        pass
            elif COMM_MODE == 2:
                send_udp("A_READY")
            elif COMM_MODE == 3:
                send_udp("A_READY")
                flush_echo()  # LoRa: 清空自回显
            elif COMM_MODE == 4:
                broadcast_udp("A_READY")
            print("A: Sent A_READY (periodic)")
            last_ready_send = now

        # 接收命令
        try:
            if COMM_MODE == 1:
                mac, msg = e.recv(0)
                if msg:
                    cmd = msg.decode().strip()
                else:
                    cmd = None
            else:
                cmd = wait_response(500)

            if cmd:
                print("A: Received: {}".format(cmd))
                if cmd == "MODE:SOFTWARE":
                    mode = "software"
                    show_info("Software mode")
                elif cmd == "MODE:HARDWARE_2WIRES":
                    mode = "hardware_2wires"
                    show_info("2-Wire Relay mode")
                elif cmd == "MODE:ADG_MULTI":
                    mode = "hardware_multi"
                    show_info("ADG2128 mode")
                elif cmd == "ESP_NOW_DISCOVER" and COMM_MODE == 1:
                    print("  收到B板发现请求，回复MAC...")
                    wlan = network.WLAN(network.STA_IF)
                    a_mac_str_local = ubinascii.hexlify(wlan.config('mac')).decode().upper()
                    response = "ESP_NOW_ACK:" + a_mac_str_local
                    try:
                        e.add_peer(mac)
                        e.send(mac, response.encode())
                        B_MAC = mac
                        print("  B板MAC已记录: {}".format(ubinascii.hexlify(B_MAC).decode().upper()))
                        show_info("B Found!")
                    except Exception as ex:
                        print("  回复失败: {}".format(ex))
                elif cmd == "PING":
                    if COMM_MODE == 3:
                        time.sleep_ms(200)
                    send_udp("PONG")
                    print("A: PONG (reply to PING)")
        except:
            pass
        time.sleep_ms(50)

    if mode == "software":
        software_mode()
    elif mode == "hardware_2wires":
        hardware_2wires_mode()
    elif mode == "hardware_multi":
        hardware_multiwires_mode()


if __name__ == "__main__":
    main()
