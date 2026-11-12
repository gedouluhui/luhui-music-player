# -*- coding: utf-8 -*-
"""sp/wasapi.py —— ctypes 直连 WASAPI：设备能力探针 ＋ 独占（bit-perfect）输出流。

为什么要有这一支（而不是继续用 PortAudio）
------------------------------------------------
`sp/audioplay.py` 的共享档走的是 PyAudioWPatch（PortAudio）。PortAudio 的 Windows WASAPI
宿主**只有共享模式**，独占要的是 `IAudioClient::Initialize(AUDCLNT_SHAREMODE_EXCLUSIVE)`。
本机装的 `PyAudioWPatch 0.2.12.9` 一共暴露 67 个名字，里面**没有一个** `PaWasapi*` 结构
（`hasattr(pa,'PaWasapiStreamInfo')` 为 False）——所以「独占输出」这一档在 Python 侧只有两条路：
换 C 库，或者自己按 vtable 调 COM。选了后者，因为它是唯一能在**不引入新二进制**的前提下
做到 bit-perfect 的路子。

风险说在明处：vtable 槽位错一个不是返回错误码，是**进程当场 AV**（本项目在「打开方式」
那条链上就这么撞过一次：槽要先解引用，错一调就 AV）。所以：

* 每个槽位号旁边都注了原文出处；原文（mingw-w64 的头文件镜像）连 sha256 一起留在
  `tools/vendor/winapi/`，判读口径写在 `tools/wasapi_abi_evidence.md`。
* 本模块**所有真调用都收在 `_call()` 这一个入口**里，槽位表收在 `_SLOT` 一个字典里；
  要复查 ABI 只需要看这两处，不用在文件里找散落的魔法数。
* 探针（`--probe`）在**子进程**里跑，就算哪天头文件镜像与实机不一致，炸掉的也是子进程，
  不是用户的播放器。`sp/audioplay.py` 只在用户真按了「独占」那一下才走开流路径。

探针绝不占有设备
----------------
`probe_device()` 只调 `GetMixFormat`／`IsFormatSupported`／`GetDevicePeriod`——这三个入口不
启动流、不锁设备、不出声。`Initialize(EXCLUSIVE)` 一成功就把那块声卡锁给我们，系统其他声音
会哑到放手为止；用户当时正在用这台电脑，那一下不是「测试」，是打断。真开流留给交还桌面之后
（`native_device` 档，由 `tools/desk_claim.py` 那一节管）。
"""
from __future__ import annotations

import ctypes
import io
import json
import struct
import sys
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- ABI 常量
# 数值来源见 tools/wasapi_abi_evidence.md；这里一行一处，改的时候必须回去对原文。
S_OK = 0x00000000
S_FALSE = 0x00000001
E_NOINTERFACE = 0x80004002
E_POINTER = 0x80004003
E_FAIL = 0x80004005
REGDB_E_CLASSNOTREG = 0x80040154
COINIT_APARTMENTTHREADED = 0x2
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = 0x80010106
# CLSCTX_INPROC_SERVER：值 1 来自 COM 规范的 CLSCTX 位定义（wtypes.h，本仓 vendor 的四个头里
# 没有它，所以这里明写它是**借来的常量**）。它在这台机器上认不认，由 tools/probe_wasapi.py 的
# 「clsctx 对照行」现场量：同一 Activate 递 1 与递 0，看回执分别是什么（对照进报告才叫实测）。
CLSCTX_INPROC_SERVER = 0x1
CO_E_NOTINITIALIZED = 0x800401F0        # 「忘了 CoInitializeEx」的样子，本机第一趟 --probe 就撞了它

# AUDCLNT_ERR(n) = MAKE_HRESULT(SEVERITY_ERROR, FACILITY_AUDCLNT, n)，FACILITY_AUDCLNT=0x889。
# ac.h 第 1841 行给了宏、没给 facility 数值（facility 在 winerror.h）。所以下面这串是**推出来的**，
# 不是抄来的：`--selftest` 里断言了推导式，`--probe` 在真机上用「一个必然不存在的采样率」把
# UNSUPPORTED_FORMAT 这个数字再核一遍（见 classify_hr 那条格子）。
def _audclnt_err(n: int) -> int:
    return (1 << 31) | (0x889 << 16) | n


AUDCLNT_E_DEVICE_INVALIDATED = _audclnt_err(0x4)        # ac.h:1847
AUDCLNT_E_UNSUPPORTED_FORMAT = _audclnt_err(0x8)        # ac.h:1851
AUDCLNT_E_DEVICE_IN_USE = _audclnt_err(0xa)             # ac.h:1853
AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED = _audclnt_err(0xe)  # ac.h:1856
AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED = _audclnt_err(0x11)   # ac.h:1859
AUDCLNT_E_EXCLUSIVE_MODE_ONLY = _audclnt_err(0x12)        # ac.h:1860
AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL = _audclnt_err(0x13)  # ac.h:1861
AUDCLNT_E_EVENTHANDLE_NOT_SET = _audclnt_err(0x14)        # ac.h:1862
AUDCLNT_E_CPUUSAGE_EXCEEDED = _audclnt_err(0x17)        # ac.h:1865
AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED = _audclnt_err(0x19)  # ac.h:1867
AUDCLNT_E_INVALID_DEVICE_PERIOD = _audclnt_err(0x20)    # ac.h:1868
AUDCLNT_E_RESOURCES_INVALIDATED = _audclnt_err(0x26)    # ac.h:1874

AUDCLNT_SHAREMODE_SHARED = 0       # audiosessiontypes.h:28-31（enum 从 0 起）
AUDCLNT_SHAREMODE_EXCLUSIVE = 1
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000   # audiosessiontypes.h:50
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000  # audiosessiontypes.h:54
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE    # ksmedia.h:721-722
KSDATAFORMAT_SUBTYPE_PCM = "00000001-0000-0010-8000-00aa00389b71"       # ksmedia.h:622
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = "00000003-0000-0010-8000-00aa00389b71"  # ksmedia.h:628
SPEAKER_2 = 0x3                     # KSAUDIO_SPEAKER_STEREO

# vtable 槽位。**唯一**允许出现「按偏移调函数」的地方就是 _call()，槽位号只在这张表里。
# IMMDevice:      Activate｜OpenPropertyStore｜GetId｜GetState
# IMMEnum:        EnumAudioEndpoints｜GetDefaultAudioEndpoint｜GetDevice｜Register…｜Unregister…
# IAudioClient:   Initialize｜GetBufferSize｜GetStreamLatency｜GetCurrentPadding｜
#                 IsFormatSupported｜GetMixFormat｜GetDevicePeriod｜Start｜Stop｜Reset｜
#                 SetEventHandle｜GetService
# IAudioRender:   GetBuffer｜ReleaseBuffer
_SLOT = {
    "unk.release": 2,
    "unk.addref": 1,
    "unk.qi": 0,
    "enum.get_default": 4,
    "enum.enum_all": 3,
    "enum.get_device": 5,
    "dev.activate": 3,
    "dev.open_props": 4,          # IMMDevice::OpenPropertyStore（mmdeviceapi.h Vtbl 段）
    "dev.get_id": 5,
    "dev.get_state": 6,
    "ps.get_value": 5,            # IPropertyStore::GetValue（propsys.h:488-527 IPropertyStoreVtbl）
    "ps.get_count": 3,            # IPropertyStore::GetCount（同一段：3 GetCount｜4 GetAt｜5 GetValue）
    "ps.get_at": 4,
    "ac.initialize": 3,
    "ac.get_buffer_size": 4,
    "ac.get_stream_latency": 5,
    "ac.get_current_padding": 6,
    "ac.is_format_supported": 7,
    "ac.get_mix_format": 8,
    "ac.get_device_period": 9,
    "ac.start": 10,
    "ac.stop": 11,
    "ac.reset": 12,
    "ac.set_event_handle": 13,
    "ac.get_service": 14,
    "rc.get_buffer": 3,
    "rc.release_buffer": 4,
    # IMMDeviceCollection：GetCount｜Item（mmdeviceapi.h 的 C++ 纯虚段，IUnknown 占 0/1/2）
    "coll.get_count": 3,
    "coll.item": 4,
}

CLSID_MMDEVICE_ENUMERATOR = "bcde0395-e52f-467c-8e3d-c4579291692e"   # mmdeviceapi.h:965
IID_IMMDEVICE_ENUMERATOR = "a95664d2-9614-4f35-a746-de8db63617e6"    # mmdeviceapi.h:582
IID_IMMDEVICE = "d666063f-1587-4e43-81f1-b948e807363f"               # mmdeviceapi.h:297
IID_IAUDIOCLIENT = "1cb9ad4c-dbfa-4c32-b178-c2f568a703b2"            # audioclient.h:201
IID_IAUDIORENDERCLIENT = "f294acfc-3146-4483-a7bf-addca7c260e2"      # audioclient.h:879

_E_RENDER = 0          # EDataFlow：eRender=0
_DEVICE_STATE_ACTIVE = 1


# ------------------------------------------------------------------------------ GUID
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    def as_str(self) -> str:
        d4 = bytes(self.Data4)
        return "%08x-%04x-%04x-%s-%s" % (
            self.Data1, self.Data2, self.Data3,
            d4[:2].hex(), d4[2:].hex())

    def __repr__(self) -> str:
        return "GUID(%s)" % self.as_str()


def guid_from(s: str) -> GUID:
    """'bcde0395-e52f-…' → GUID。Data4 是 8 个**单字节**，不能按 big-endian u64 读——
    这是手写 COM 最常错的一处，所以它单独有一条已知答案格子。"""
    h = s.replace("{", "").replace("}", "").replace("-", "")
    if len(h) != 32:
        raise ValueError("GUID 长度不对：%r" % (s,))
    g = GUID()
    g.Data1 = int(h[0:8], 16)
    g.Data2 = int(h[8:12], 16)
    g.Data3 = int(h[12:16], 16)
    for i in range(8):
        g.Data4[i] = int(h[16 + i * 2:18 + i * 2], 16)
    return g


# ------------------------------------------------------------------ WAVEFORMATEX 家族
class WAVEFORMATEX(ctypes.Structure):
    """原文：mmreg.h 第 14 行 `#include "pshpack1.h"`、第 2094–2102 行的七个字段。
    整包对齐是 **1**，所以尺寸就是 7 个字段的和 18，尾上没有填充。
    不写 _pack_ 的话 ctypes 按自然对齐补到 20，于是 byref 交出去的字节流和驱动读的
    差两个填充字节——表现为协商直接给 AUDCLNT_E_UNSUPPORTED_FORMAT，而不是报「布局错」，
    这种失败特别容易被误读成「这块卡不支持独占」。"""
    _pack_ = 1
    _fields_ = [("wFormatTag", ctypes.c_uint16),
                ("nChannels", ctypes.c_uint16),
                ("nSamplesPerSec", ctypes.c_uint32),
                ("nAvgBytesPerSec", ctypes.c_uint32),
                ("nBlockAlign", ctypes.c_uint16),
                ("wBitsPerSample", ctypes.c_uint16),
                ("cbSize", ctypes.c_uint16)]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    """原文：ksmedia.h 第 699 行 `#include <pshpack1.h>`、第 706–718 行的四个成员
    （Format / Samples 联合 / dwChannelMask / SubFormat）。尺寸 40，cbSize＝40−18＝22。
    四个偏移（0/18/20/24）是驱动直接读的，所以每个都有一条已知答案格子。"""
    _pack_ = 1
    _fields_ = [("Format", WAVEFORMATEX),
                ("wValidBitsPerSample", ctypes.c_uint16),
                ("dwChannelMask", ctypes.c_uint32),
                ("SubFormat", GUID)]


def make_wf(rate: int, channels: int = 2, bits: int = 32,
            subtype: str = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT,
            container_bits: Optional[int] = None) -> WAVEFORMATEXTENSIBLE:
    """造一个 WAVEFORMATEXTENSIBLE。`bits` 是**有效位**，`container_bits` 是**容器位**
    （24-bit 常见形态是「3 字节有效、装进 4 字节容器」；给 None 就按 8 的向上取整）。
    blockAlign/avgBytes 一律按容器算——按有效位算会得到一个驱动必拒的格式。"""
    cont = container_bits or (32 if bits > 16 else 16)
    if cont < bits:
        raise ValueError("容器位 %d 装不下有效位 %d" % (cont, bits))
    wf = WAVEFORMATEXTENSIBLE()
    wf.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE
    wf.Format.nChannels = channels
    wf.Format.nSamplesPerSec = rate
    wf.Format.nBlockAlign = (cont // 8) * channels
    wf.Format.nAvgBytesPerSec = rate * wf.Format.nBlockAlign
    wf.Format.wBitsPerSample = cont
    wf.Format.cbSize = ctypes.sizeof(WAVEFORMATEXTENSIBLE) - ctypes.sizeof(WAVEFORMATEX)
    wf.wValidBitsPerSample = bits
    wf.dwChannelMask = SPEAKER_2 if channels == 2 else 0
    wf.SubFormat = guid_from(subtype)
    return wf


def wf_name(wf: WAVEFORMATEXTENSIBLE) -> str:
    """给人看的一行格式名：`f32@48000/2h`、`pcm24@96000/2h`。
    数字取**有效位**（不是容器位），因为「24 位」是用户关心的那一层；容器位单独在
    state 回执里给（`container_bits`），两者不等时（24-in-32）必须能分别看见。"""
    sub = wf.SubFormat.as_str()
    kind = {KSDATAFORMAT_SUBTYPE_IEEE_FLOAT: "f",
            KSDATAFORMAT_SUBTYPE_PCM: "pcm"}.get(sub, sub)
    return "%s%d@%d/%dh" % (kind, wf.wValidBitsPerSample, wf.Format.nSamplesPerSec,
                            wf.Format.nChannels)


def read_wf(ptr: int) -> Optional[WAVEFORMATEXTENSIBLE]:
    """把 COM 交回来的 WAVEFORMATEX* 读成结构。cbSize>0 的按 EXTENSIBLE 读（前 18 字节
    两个布局是一样的，所以 cbSize==0 时只填得进 WAVEFORMATEX 那几个字段）。"""
    if not ptr:
        return None
    wfe = WAVEFORMATEX.from_address(ptr)
    out = WAVEFORMATEXTENSIBLE()
    ctypes.memmove(ctypes.byref(out), ptr, ctypes.sizeof(WAVEFORMATEX))
    if wfe.cbSize >= 22:
        ctypes.memmove(ctypes.byref(out), ptr, ctypes.sizeof(WAVEFORMATEXTENSIBLE))
    else:                                   # 非 extensible：按 tag 反推子格式
        out.wValidBitsPerSample = wfe.wBitsPerSample
        out.dwChannelMask = SPEAKER_2 if wfe.nChannels == 2 else 0
        out.SubFormat = guid_from(
            KSDATAFORMAT_SUBTYPE_IEEE_FLOAT if wfe.wFormatTag == WAVE_FORMAT_IEEE_FLOAT
            else KSDATAFORMAT_SUBTYPE_PCM)
    return out


# -------------------------------------------------------- 端点友好名（PropertyStore）
class PROPERTYKEY(ctypes.Structure):
    """wtypes.h:573-576 `struct _tagpropertykey { GUID fmtid; DWORD pid; }`。"""
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]


class PROPVARIANT(ctypes.Structure):
    """字节布局按 propidl.h 第 224–309 行（已下载留底，sha 见 tools/wasapi_abi_evidence.md）：

        struct tagPROPVARIANT {
          union {
            struct tag_inner_PROPVARIANT {
              VARTYPE vt;                 // 偏移 0（2 字节）
              PROPVAR_PAD1 wReserved1;    // 偏移 2
              PROPVAR_PAD2 wReserved2;    // 偏移 4
              PROPVAR_PAD3 wReserved3;    // 偏移 6
              union { …; LPWSTR pwszVal; … }   // 偏移 8 ⇒ pv 就落在这里
            };
            DECIMAL decVal;               // 外层再与一个 16 字节的 DECIMAL 并集
          };
        };

    三个 pad 取 WORD 那一支（第 217–219 行；BYTE/BYTE/ULONG 那一支在第 212 行的 `#if 0` 里，
    两支算出来的值联合偏移都是 8，所以这一格不依赖「哪支被编译」。）
    值联合里最宽的成员是 `ULONG cElems; T *pElems;` 那一批（第 193–198 行 CALPWSTR 等），
    x64 上 4 + 补 4 + 指针 8 ＝ 16 ⇒ 整条结构 **24 字节**（8＋16，外层 DECIMAL 只有 16，不更大）。

    为什么非得给满 24：`GetValue` 是按 24 字节写的，早先这里只声明到 `pv`（16 字节），
    等于让它往我这块 ctypes 内存后面再写 8 字节——写花的是邻居对象，不是「读不到名字」那么简单。
    """
    _fields_ = [("vt", ctypes.c_uint16),
                ("r1", ctypes.c_uint16), ("r2", ctypes.c_uint16), ("r3", ctypes.c_uint16),
                ("pv", ctypes.c_void_p),
                ("tail", ctypes.c_uint64)]      # 值联合的后半（CAC/CALPWSTR 那批 16 字节成员）


VT_LPWSTR = 31            # wtypes.h:544
IID_IPROPERTYSTORE = "886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"   # propsys.h:460 DEFINE_GUID
# 友好名这对键**已在本机实测**（tools/wasapi_names.probe.json，rc=0，6 个渲染端点全部读到名字，
# 且与 pyaudiowpatch 那份独立实现的名字**多重集相等**：agree=true、only_com=[]、only_pa=[]）：
#   fmtid a45c254e… ＋ pid 14 → 「扬声器 (EDIFIER N500)」；同 fmtid 的 pid 2 是「扬声器」（设备本地名），
#   两者不是同一个东西，所以不能拿 2 当友好名。propkey.h 里这批键只列到 10/13/17/29/3/37，
#   **没有 14**——14 原本是借来的，现在是量出来的。
PKEY_DEVICE_FRIENDLY_NAME_FMTID = "a45c254e-df1c-4efd-8020-67d146a850e0"
PKEY_DEVICE_FRIENDLY_NAME_PID = 14
# 自己这一侧的失败也要有码：拿「0」当「没找到 PropVariantClear」会和真 S_OK 混成一片，
# 而这两个结局的处置完全不同（一个是 COM 里没有入口，一个是「成功了，名字却读不出来」）。
E_PROPVAR_CLEAR_NOT_FOUND = 0x10000001
E_PROPVAR_CLEAR_RAISED = 0x10000002
E_READ_PROP_RAISED = 0x10000003


def _propvariant_clear(pv_ptr: int) -> int:
    """PropVariantClear：优先 propsys.dll，退化到 oleaut32.dll（两处都导出）。
    回它的 HRESULT（没入口＝E_PROPVAR_CLEAR_NOT_FOUND，入口崩＝E_PROPVAR_CLEAR_RAISED）——
    名字读不出来那趟要分「是我不认识回来的 vt」还是「释放失败」，所以错误码不许咽进 None。
    释放**只能**走它：里面的宽字符串是 COM 分的，自己 CoTaskMemFree 就是拿错的分配器
    去放邻居的内存（--probe 第一趟的 STATUS_HEAP_CORRUPTION 就是这么来的）。"""
    for dll in ("propsys", "oleaut32"):
        try:
            fn = getattr(ctypes.windll.LoadLibrary("%s.dll" % dll), "PropVariantClear")
        except Exception:
            continue
        fn.argtypes = [ctypes.POINTER(PROPVARIANT)]
        fn.restype = ctypes.c_long
        try:
            return to_hr(fn(ctypes.cast(pv_ptr, ctypes.POINTER(PROPVARIANT))))
        except Exception:
            return E_PROPVAR_CLEAR_RAISED
    return E_PROPVAR_CLEAR_NOT_FOUND


def _open_property_store(dev_ptr: int, arity: int = 2) -> Tuple[int, int]:
    """IMMDevice::OpenPropertyStore(STGM_READ) → (HRESULT, IPropertyStore 指针)。

    **arity 这条分叉必须实测，不能按印象选**：本机留底的 mmdeviceapi.h 三处口径一致地写两参
    （第 308–310 行 C++ 纯虚段、第 346–349 行 Vtbl 段、第 374/392 行的宏与 inline 包装：
    `OpenPropertyStore(This, stgmAccess, IPropertyStore **ppProperties)`），
    而微软文档里那条签名多一个 `REFIID riid`。两份对不上时探针两形态各跑一行，谁给 S_OK
    谁回可用指针，就把结论写进 tools/wasapi_abi_evidence.md（槽号都是 4，错的只是参数个数）。
    """
    ps = ctypes.c_void_p(0)
    if arity == 2:
        hr = to_hr(_call(dev_ptr, "dev.open_props", ctypes.c_long,
                         [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                         [0, ctypes.byref(ps)]))            # STGM_READ = 0
    else:
        iid = guid_from(IID_IPROPERTYSTORE)
        hr = to_hr(_call(dev_ptr, "dev.open_props", ctypes.c_long,
                         [ctypes.c_uint, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                         [0, ctypes.byref(iid), ctypes.byref(ps)]))
    return hr, (ps.value or 0)


def read_prop(ps: int, fmtid: str, pid: int) -> Dict[str, Any]:
    """IPropertyStore::GetValue 的**带回执**版本：回 {hr, vt, str, clear}，不吞错误码。"""
    key = PROPERTYKEY()
    key.fmtid = guid_from(fmtid)
    key.pid = pid
    pv = PROPVARIANT()
    try:
        hr = to_hr(_call(ps, "ps.get_value", ctypes.c_long,
                         [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)],
                         [ctypes.byref(key), ctypes.byref(pv)]))
        s = None
        if hr == S_OK and pv.vt == VT_LPWSTR and pv.pv:
            s = ctypes.wstring_at(pv.pv)
        return {"hr": hr, "vt": int(pv.vt), "str": s,
                "clear": _propvariant_clear(ctypes.addressof(pv))}
    except Exception as e:
        return {"hr": E_READ_PROP_RAISED, "vt": -1, "str": None, "clear": 0,
                "err": "%s: %s" % (type(e).__name__, e)}


def device_props(dev_ptr: int, max_props: int = 128) -> Dict[str, Any]:
    """把一个端点的属性存储**整个枚举一遍**：GetCount → GetAt(i) → GetValue。

    这一条是给「pid＝14 到底是不是友好名」兜底的：propkey.h 里那批同 fmtid 的键
    （第 533–575 行：ClassGuid=10、CompatibleIds=4、DeviceManufacturer=13、HardwareIds=3、
    DeviceCapabilities=17、DeviceCharacteristics=29、LocationPaths=37）**唯独没有 pid 14**，
    所以 14 是从文档借的。枚举能把「哪一号键装着扬声器那行字」直接量出来，
    再和 pyaudiowpatch 自己列的名字对多重集——借来的常数当场变成实测。
    槽号：propsys.h:488-527 的 IPropertyStoreVtbl ⇒ 3 GetCount｜4 GetAt｜5 GetValue。
    """
    out: Dict[str, Any] = {"items": []}
    hr, ps = _open_property_store(dev_ptr)
    out["open_hr"] = hr
    if hr != S_OK or not ps:
        # 两参形态打不开就当场试三参（不另开一趟进程）：这一格分叉正是上面那条注释要实测的东西
        hr3, ps3 = _open_property_store(dev_ptr, arity=3)
        out["open_hr_arity3"] = hr3
        if hr3 != S_OK or not ps3:
            return out
        hr, ps = hr3, ps3
    try:
        n = ctypes.c_uint(0)
        h = to_hr(_call(ps, "ps.get_count", ctypes.c_long,
                        [ctypes.POINTER(ctypes.c_uint)], [ctypes.byref(n)]))
        out["count_hr"] = h
        out["count"] = int(n.value)
        if h != S_OK:
            return out
        for i in range(min(int(n.value), max_props)):
            key = PROPERTYKEY()
            h2 = to_hr(_call(ps, "ps.get_at", ctypes.c_long,
                             [ctypes.c_uint, ctypes.POINTER(PROPERTYKEY)],
                             [i, ctypes.byref(key)]))
            if h2 != S_OK:
                out["items"].append({"i": i, "get_at_hr": h2})
                continue
            v = read_prop(ps, key.fmtid.as_str(), int(key.pid))
            out["items"].append({"i": i, "fmtid": key.fmtid.as_str(), "pid": int(key.pid),
                                 "hr": v["hr"], "vt": v["vt"],
                                 "str": (v["str"][:120] if v["str"] is not None else None)})
        return out
    finally:
        _release(ps)


def device_name_ex(dev_ptr: int) -> Dict[str, Any]:
    """友好名 + 全过程回执（哪种 arity 打开的、GetValue 的 hr、回来的 vt、PropVariantClear 的 hr）。
    名字读不出来时，这几行就是答案；只回 None 等于把线索扔了。"""
    diag: Dict[str, Any] = {"name": None, "tries": []}
    for arity in (2, 3):
        hr, ps = _open_property_store(dev_ptr, arity)
        if hr != S_OK or not ps:
            diag["tries"].append({"arity": arity, "open_hr": hr, "open": classify_hr(hr)})
            continue
        try:
            v = read_prop(ps, PKEY_DEVICE_FRIENDLY_NAME_FMTID, PKEY_DEVICE_FRIENDLY_NAME_PID)
            diag["tries"].append({"arity": arity, "open_hr": hr, "get_hr": v["hr"],
                                  "get": classify_hr(v["hr"]), "vt": v["vt"],
                                  "clear": v["clear"], "str": v["str"],
                                  "err": v.get("err")})
            if v["str"]:
                diag["name"] = v["str"]
                diag["arity"] = arity
                return diag
        finally:
            _release(ps)
    return diag


def device_name(dev_ptr: int) -> Optional[str]:
    """OpenPropertyStore(STGM_READ) + GetValue(PKEY_Device_FriendlyName) → 友好名。
    成功时与 device_name_ex 同一条代码路径（不另写一份，免得「产品走的那条从没被量过」）。"""
    return device_name_ex(dev_ptr)["name"]


# --------------------------------------------------------------------- HRESULT 归类
HR_NAME = {
    S_OK: "S_OK", S_FALSE: "S_FALSE", E_NOINTERFACE: "E_NOINTERFACE",
    E_POINTER: "E_POINTER", E_FAIL: "E_FAIL",
    REGDB_E_CLASSNOTREG: "REGDB_E_CLASSNOTREG",
    CO_E_NOTINITIALIZED: "CO_E_NOTINITIALIZED",
    RPC_E_CHANGED_MODE: "RPC_E_CHANGED_MODE",
    AUDCLNT_E_UNSUPPORTED_FORMAT: "AUDCLNT_E_UNSUPPORTED_FORMAT",
    AUDCLNT_E_DEVICE_IN_USE: "AUDCLNT_E_DEVICE_IN_USE",
    AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED: "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED",
    AUDCLNT_E_EXCLUSIVE_MODE_ONLY: "AUDCLNT_E_EXCLUSIVE_MODE_ONLY",
    AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED: "AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED",
    AUDCLNT_E_EVENTHANDLE_NOT_SET: "AUDCLNT_E_EVENTHANDLE_NOT_SET",
    AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL: "AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL",
    AUDCLNT_E_RESOURCES_INVALIDATED: "AUDCLNT_E_RESOURCES_INVALIDATED",
    AUDCLNT_E_DEVICE_INVALIDATED: "AUDCLNT_E_DEVICE_INVALIDATED",
    AUDCLNT_E_CPUUSAGE_EXCEEDED: "AUDCLNT_E_CPUUSAGE_EXCEEDED",
    AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED: "AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED",
    AUDCLNT_E_INVALID_DEVICE_PERIOD: "AUDCLNT_E_INVALID_DEVICE_PERIOD",
}


def to_hr(v: int) -> int:
    """COM 的 HRESULT 是有符号 32 位；ctypes 回来可能是负数。统一成无符号再比。"""
    return v & 0xFFFFFFFF


def classify_hr(v: int) -> str:
    return HR_NAME.get(to_hr(v), "0x%08x" % to_hr(v))


def hr_is_ok(v: int) -> bool:
    return to_hr(v) == S_OK


# COM 调用入口：整份文件里**只有这里**按槽位取函数指针。
def _slot(this: int, slot: int) -> int:
    # 先验句柄再碰内存：`c_void_p.from_address(0)` 不是返回 None，是**解引用空指针**。
    # 一个 0 句柄（激活失败/已释放）在这里必须变成可读的异常，不能变成 AV——
    # 这条是本模块存在的理由之一：错一个槽位、错一次解引用，死的是用户的播放器。
    if not this:
        raise RuntimeError("COM 句柄为空（this=0），不去解引用第 %d 槽" % slot)
    vtbl = ctypes.c_void_p.from_address(this).value
    if not vtbl:
        raise RuntimeError("COM 对象指针 vtable 为空（this=0x%x）" % this)
    fn = ctypes.c_void_p.from_address(vtbl + slot * 8).value
    if not fn:
        raise RuntimeError("vtable 槽 %d 是空指针（this=0x%x）" % (slot, this))
    return fn


def _call(this: int, name: str, restype, argtypes, args):
    """`name` 必须能在 _SLOT 里查到——写错名字立刻 KeyError，而不是悄悄调错槽。
    argtypes 全部给齐（本项目在 GDI 那条链上吃过「没给 argtypes 于是清理阶段
    OverflowError」的亏，规矩：句柄/指针的进出都要带类型）。"""
    fn = _slot(this, _SLOT[name])
    proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(fn)(ctypes.c_void_p(this), *args)


def _release(this: int) -> None:
    if this:
        try:
            _call(this, "unk.release", ctypes.c_ulong, [], [])
        except Exception:
            pass


class WasapiError(RuntimeError):
    def __init__(self, msg: str, hr: Optional[int] = None):
        super().__init__(msg)
        self.hr = hr
        self.hr_name = classify_hr(hr) if hr is not None else None


# ------------------------------------------------------------------ COM 生命周期
_ole32 = None
_kernel32 = None


def _libs():
    global _ole32, _kernel32
    if _ole32 is None:
        import platform
        if platform.system() != "Windows":
            raise WasapiError("WASAPI 只在 Windows 上存在（当前 %s）" % platform.system())
        _ole32 = ctypes.windll.ole32
        _kernel32 = ctypes.windll.kernel32
        _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _ole32.CoInitializeEx.restype = ctypes.c_long
        _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        _ole32.CoTaskMemFree.restype = None
        _ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                            ctypes.c_uint, ctypes.POINTER(GUID),
                                            ctypes.POINTER(ctypes.c_void_p)]
        _ole32.CoCreateInstance.restype = ctypes.c_long
        _kernel32.CreateEventExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_uint, ctypes.c_uint]
        _kernel32.CreateEventExW.restype = ctypes.c_void_p
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32.CloseHandle.restype = ctypes.c_int
        _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _kernel32.WaitForSingleObject.restype = ctypes.c_uint
    return _ole32, _kernel32


def com_init(mta: bool = True) -> str:
    """返回 'initialized'/'already'/'changed-mode'。`RPC_E_CHANGED_MODE` 不报错：
    意思是这个线程已被人按别模型初始化过（PyQt 会这么干），继续用它现在的模型即可。"""
    ole32, _ = _libs()
    hr = to_hr(ole32.CoInitializeEx(None, COINIT_MULTITHREADED if mta
                                   else COINIT_APARTMENTTHREADED))
    if hr == S_OK:
        return "initialized"
    if hr in (S_FALSE, RPC_E_CHANGED_MODE):
        return "already"
    raise WasapiError("CoInitializeEx 失败", hr)


def enumerator() -> int:
    ole32, _ = _libs()
    # COM 要先按线程初始化，否则 CoCreateInstance 直接给 CO_E_NOTINITIALIZED(0x800401F0)。
    # 这条不是防御性代码：真机第一趟 --probe 就是这么红的（tools/wasapi_probe.first.json
    # 里那句「失败：0x800401f0」），修之前它连设备列表都拿不到。
    com_init(mta=True)
    cls = guid_from(CLSID_MMDEVICE_ENUMERATOR)
    iid = guid_from(IID_IMMDEVICE_ENUMERATOR)
    p = ctypes.c_void_p(0)
    hr = to_hr(ole32.CoCreateInstance(ctypes.byref(cls), None, CLSCTX_INPROC_SERVER,
                                      ctypes.byref(iid), ctypes.byref(p)))
    if hr != S_OK:
        # 这一步同时是「CLSID/IID 在这台机器上真实存在」的机器侧证据：错了会是
        # REGDB_E_CLASSNOTREG / E_NOINTERFACE，而不是崩。
        raise WasapiError("CoCreateInstance(MMDeviceEnumerator) 失败：%s"
                          % classify_hr(hr), hr)
    return p.value


def device_ids(mask: int = _DEVICE_STATE_ACTIVE) -> List[Dict[str, Any]]:
    """列出渲染端点。用 EnumAudioEndpoints(eRender, mask)。"""
    ole32, _ = _libs()
    en = enumerator()
    try:
        coll = ctypes.c_void_p(0)
        hr = to_hr(_call(en, "enum.enum_all", ctypes.c_long,
                         [ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                         [_E_RENDER, mask, ctypes.byref(coll)]))
        if hr != S_OK:
            raise WasapiError("EnumAudioEndpoints 失败：%s" % classify_hr(hr), hr)
        out: List[Dict[str, Any]] = []
        try:
            n = ctypes.c_uint(0)
            # IMMDeviceCollection 的 GetCount/Item 也走同一张 _SLOT（mmdeviceapi.h:432-458 的
            # Vtbl 段：0/1/2 是 IUnknown，3 GetCount，4 Item）——不留裸偏移在这里。
            hr = to_hr(_call(coll.value, "coll.get_count", ctypes.c_long,
                             [ctypes.POINTER(ctypes.c_uint)], [ctypes.byref(n)]))
            if hr != S_OK:
                raise WasapiError("IMMDeviceCollection::GetCount 失败：%s" % classify_hr(hr), hr)
            for i in range(n.value):
                dev = ctypes.c_void_p(0)
                h2 = to_hr(_call(coll.value, "coll.item", ctypes.c_long,
                                 [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                                 [i, ctypes.byref(dev)]))
                if h2 != S_OK or not dev.value:
                    continue
                try:
                    # GetId 交回来的是**驱动用 CoTaskMemAlloc 分的宽字符串**。必须按裸地址接：
                    # 先 .value 解码成 Python str、再 cast(str) 去 CoTaskMemFree， freeing 的是
                    # CPython 自己的对象内存——真机第一趟就是这么死的（STATUS_HEAP_CORRUPTION
                    # 0xc0000374，rc=127、stderr 全空）。文本要读，但读和放用的是同一个裸指针。
                    pp = ctypes.c_void_p(0)
                    h3 = to_hr(_call(dev.value, "dev.get_id", ctypes.c_long,
                                     [ctypes.POINTER(ctypes.c_void_p)],
                                     [ctypes.byref(pp)]))
                    if h3 == S_OK and pp.value:
                        nd = device_name_ex(dev.value)
                        row = {"index": i, "id": ctypes.wstring_at(pp.value),
                               "name": nd["name"]}
                        if nd["name"] is None:
                            # 名字读不出来时把线索留下（否则 JSON 里只有一列 None，
                            # 分不清「OpenPropertyStore 的 arity 不对」「pid 不对」「vt 不认识」）
                            row["name_diag"] = nd["tries"]
                        out.append(row)
                    if pp.value:
                        ole32.CoTaskMemFree(pp.value)
                finally:
                    _release(dev.value)
        finally:
            _release(coll.value)
        return out
    finally:
        _release(en)


def default_device_id(role: int = 0) -> Dict[str, Any]:
    """系统默认渲染端点（GetDefaultAudioEndpoint(eRender, role)）。
    role：0=eConsole（播放音乐用的那一档）、1=eMultimedia、2=eCommunications。
    回 {"id","name"}；失败抛 WasapiError（上层据此回落共享，不冒充独占）。
    槽号 4 来自 mmdeviceapi.h 的 Vtbl 段：3 EnumAudioEndpoints｜**4 GetDefaultAudioEndpoint**｜5 GetDevice。
    """
    en = enumerator()
    try:
        dev = ctypes.c_void_p(0)
        hr = to_hr(_call(en, "enum.get_default", ctypes.c_long,
                         [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                         [_E_RENDER, role, ctypes.byref(dev)]))
        if hr != S_OK or not dev.value:
            raise WasapiError("GetDefaultAudioEndpoint 失败：%s" % classify_hr(hr), hr)
        try:
            pp = ctypes.c_void_p(0)
            h2 = to_hr(_call(dev.value, "dev.get_id", ctypes.c_long,
                             [ctypes.POINTER(ctypes.c_void_p)], [ctypes.byref(pp)]))
            if h2 != S_OK or not pp.value:
                raise WasapiError("GetId 失败：%s" % classify_hr(h2), h2)
            try:
                did = ctypes.wstring_at(pp.value)
            finally:
                _libs()[0].CoTaskMemFree(pp.value)
            return {"id": did, "name": device_name(dev.value), "role": role}
        finally:
            _release(dev.value)
    finally:
        _release(en)


def _device_ptr(device_id: str) -> int:
    """GetDevice(id) → IMMDevice 裸指针（调用方负责 _release）。"""
    en = enumerator()
    try:
        dev = ctypes.c_void_p(0)
        hr = to_hr(_call(en, "enum.get_device", ctypes.c_long,
                         [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)],
                         [device_id, ctypes.byref(dev)]))
        if hr != S_OK:
            raise WasapiError("GetDevice(%s) 失败：%s" % (device_id, classify_hr(hr)), hr)
        return dev.value or 0
    finally:
        _release(en)


def activate(device_id: str, iid_str: str, clsctx: int = CLSCTX_INPROC_SERVER,
             params=None) -> Tuple[int, int]:
    """IMMDevice::Activate 的**唯一**入口，返回 (HRESULT, 接口指针)。
    AudioClient 和「对照行」共用这一条，所以对照才真是同一条代码路径的对照。

    这里踩过一次实机红：mmdeviceapi.h 的 Vtbl 段写的是
    `Activate(iid, dwClsCtx, PROPVARIANT *pActivationParams, void **ppv)`——This 之外**四个**入参，
    没有 clsid。早先多递了一个「byref(iid) 当 clsid」，x64 上从 RDX 起整列右移一格：
    dwClsCtx 收到指针低 32 位、pActivationParams 收到 1、ppv 收到 None ⇒ E_POINTER。
    槽号(3)是对的、参数个数错了也会是同一个错误码，所以 arity 也得机器核（tools/wasapi_abi_check.py）。
    """
    dev = _device_ptr(device_id)
    try:
        iid = guid_from(iid_str)
        pp = ctypes.c_void_p(0)
        hr = to_hr(_call(dev, "dev.activate", ctypes.c_long,
                         [ctypes.POINTER(GUID), ctypes.c_uint, ctypes.c_void_p,
                          ctypes.POINTER(ctypes.c_void_p)],
                         [ctypes.byref(iid), clsctx, params, ctypes.byref(pp)]))
        return hr, (pp.value or 0)
    finally:
        _release(dev)


class AudioClient:
    """一个端点的 IAudioClient。**只是能力查询用**；开独占流走 ExclusiveStream。"""

    def __init__(self, device_id: str):
        ole32, _ = _libs()
        self.device_id = device_id
        hr, p = activate(device_id, IID_IAUDIOCLIENT)
        if hr != S_OK or not p:
            raise WasapiError("Activate(IAudioClient) 失败：%s" % classify_hr(hr), hr)
        self.p = p
        self._ole = ole32

    def close(self) -> None:
        _release(self.p)
        self.p = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- 不占有设备的三个入口 ----
    def mix_format(self) -> Optional[WAVEFORMATEXTENSIBLE]:
        pp = ctypes.c_void_p(0)
        hr = to_hr(_call(self.p, "ac.get_mix_format", ctypes.c_long,
                         [ctypes.POINTER(ctypes.c_void_p)], [ctypes.byref(pp)]))
        if hr != S_OK:
            raise WasapiError("GetMixFormat 失败：%s" % classify_hr(hr), hr)
        try:
            return read_wf(pp.value)
        finally:
            self._ole.CoTaskMemFree(pp)

    def device_period(self) -> Tuple[int, int]:
        a, b = ctypes.c_longlong(0), ctypes.c_longlong(0)
        hr = to_hr(_call(self.p, "ac.get_device_period", ctypes.c_long,
                         [ctypes.POINTER(ctypes.c_longlong), ctypes.POINTER(ctypes.c_longlong)],
                         [ctypes.byref(a), ctypes.byref(b)]))
        if hr != S_OK:
            raise WasapiError("GetDevicePeriod 失败：%s" % classify_hr(hr), hr)
        return a.value, b.value

    def is_format_supported(self, share: int, wf: WAVEFORMATEXTENSIBLE) -> Tuple[int, Optional[WAVEFORMATEXTENSIBLE]]:
        closest = ctypes.c_void_p(0)
        hr = to_hr(_call(self.p, "ac.is_format_supported", ctypes.c_long,
                         [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                         [share, ctypes.byref(wf), ctypes.byref(closest)]))
        cm = None
        if closest.value:
            try:
                cm = read_wf(closest.value)
            finally:
                self._ole.CoTaskMemFree(closest)
        return hr, cm


# ------------------------------------------------------------- 候选格式与协商（纯函数）
RATES = (44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000)
CANDIDATES: Tuple[Tuple[str, int, str, int], ...] = tuple(
    [("%s-f32" % r, r, KSDATAFORMAT_SUBTYPE_IEEE_FLOAT, 32) for r in RATES] +
    [("%s-pcm16" % r, r, KSDATAFORMAT_SUBTYPE_PCM, 16) for r in RATES] +
    [("%s-pcm24" % r, r, KSDATAFORMAT_SUBTYPE_PCM, 24) for r in RATES] +
    [("%s-pcm32" % r, r, KSDATAFORMAT_SUBTYPE_PCM, 32) for r in RATES])


def candidates_for(ch: int) -> List[Tuple[str, WAVEFORMATEXTENSIBLE]]:
    out = []
    for tag, rate, sub, bits in CANDIDATES:
        cont = 32 if bits == 24 else bits          # 24 位一律按「3 有效位装进 4 字节容器」探
        out.append((tag, make_wf(rate, ch, bits, sub, cont)))
    return out


def pick_exclusive_format(supported: List[Dict[str, Any]], source_rate: int,
                          target_rate: int, source_bits: int = 24
                          ) -> Dict[str, Any]:
    """从「驱动说支持的独占格式」里挑一个开流参数。纯函数，所以能在锁屏/没声卡时判。

    优先级（这是「最专业」那条口径的具体化）：
      1. 采样率 == 源采样率 且有 ≥ 源位深的整数/浮点格式 ⇒ **真 bit-perfect**（不重采样、不加位）
      2. 采样率 == 源采样率，但驱动只给到比源浅的容器 ⇒ 不重采样、**要量化**（不算 bit-perfect）
      3. 没有与源同率的格式 ⇒ 退到 target_rate（或最近的候选），**要重采样**（不算 bit-perfect）
      4. 上面都没有 ⇒ 回 mode=shared，把原因交回上层（不许冒充独占成功）
    返回 {"why","mode","rate","bits","container","tag","bit_perfect","resampled"}；
    mode ∈ {exclusive,shared}。
    为什么 bit_perfect/resampled 必须是**布尔字段**而不只是 why 里那句话：上层界面要说
    「现在是/不是 bit-perfect」，让 UI 去匹配中文子串等于把一条结论绑在文案上——改一个字
    就悄悄翻面。why 给人看，布尔给判据看，两条都得有。
    """
    ok = [s for s in supported if s.get("hr") == S_OK]
    if not ok:
        return {"why": "驱动对全部候选独占格式都说不（或一个都没探到）", "mode": "shared",
                "rate": None, "bits": None, "container": None, "tag": None,
                "bit_perfect": False, "resampled": None}

    def better(a, b):                 # 位深够 ＋ 采样率贴源 ＝ 更好
        def score(s):
            d = 0 if s["valid_bits"] >= source_bits else 1000
            r = 0 if s["rate"] == source_rate else (10 if s["rate"] == target_rate else 20)
            # 源位深**知道**时，在同率且位深够的几档里选「最浅的够用档」：16 位的 CD rip
            #   开 16 位原生容器就够，塞进 24-in-32 数据一个字节都不变、却把总线字节翻倍；
            # 源位深**不知道**（0）时反过来选最深那档：万一它是 24 位，别在开流这一步就先削掉。
            depth = (-s["valid_bits"]) if source_bits <= 0 else s["valid_bits"]
            return (d, r, depth, -s["rate"])
        return a if score(a) <= score(b) else b

    def rec(s, why, bp, resampled):
        return {"why": why, "mode": "exclusive", "rate": s["rate"], "bits": s["valid_bits"],
                "container": s["container_bits"], "tag": s["tag"],
                "bit_perfect": bp, "resampled": resampled}

    exact = [s for s in ok if s["rate"] == source_rate and s["valid_bits"] >= source_bits]
    if exact:
        best = exact[0]
        for s in exact[1:]:
            best = better(best, s)
        return rec(best, "采样率与源一致、位深不劣于源 ⇒ 不重采样不加深（bit-perfect）",
                   True, False)

    # 第二出口：同率但比源浅。这一档**不重采样**，所以绝不能写进「没有与源同率的格式」
    # 那句话里 —— 本机实测就是这样撞上的：默认端点的独占清单只有 16 位三档
    # （44100/48000/96000-pcm16，tools/exclusive_probe.20261020-002924.json），
    # 源按 24 位处理时第一出口为空，若把它并到第三出口去说「没有与源同率的独占格式、
    # 仍要重采样」，界面就会对着一台明明同率的设备撒谎。
    same = [s for s in ok if s["rate"] == source_rate]
    if same:
        best = sorted(same, key=lambda s: (-s["valid_bits"], -s["container_bits"]))[0]
        return rec(best,
                   "设备在源采样率（%d Hz）上只给到 %d 位容器，而源按 %d 位处理 ⇒ 不重采样，"
                   "但要把样本量化到 %d 位（不是 bit-perfect）"
                   % (source_rate, best["valid_bits"], source_bits, best["valid_bits"]),
                   False, False)

    near = [s for s in ok if s["rate"] == target_rate] or ok

    def score(s):
        return abs(s["rate"] - target_rate), -s["valid_bits"]
    best = sorted(near, key=score)[0]
    return rec(best,
               "没有与源同率的独占格式，退到 %d Hz ⇒ 这一路仍要重采样（不算 bit-perfect）"
               % best["rate"], False, True)


def hns_for(rate: int, frames: int) -> int:
    """帧 → 100 ns 单位（REFERENCE_TIME）。整除会丢尾，独占的缓冲时长要**向上**取整，
    否则算出的周期比驱动要求的短，Initialize 给 AUDCLNT_E_INVALID_DEVICE_PERIOD。"""
    if rate <= 0 or frames < 0:
        raise ValueError("rate/frames 不合法：%r %r" % (rate, frames))
    return -(-frames * 10_000_000 // rate)


def block_frames(period_hns: int, rate: int) -> int:
    """设备周期（100ns）→ 一周期多少帧，向上取整。"""
    return max(1, -(-period_hns * rate // 10_000_000))


def frames_to_device(out: "Any", bits: int, container: int,
                     dither: bool = False, rng: "Any" = None) -> bytes:
    """float32 [-1,1] → 设备容器字节。只在**必须**转（设备只给整型独占）时走这里。

    24-in-32：小端 4 字节里高位是有效位，所以是 `round(v*2^23)` 左移 (container-24) 位；
    写错这个移位量就是「音量差 256 倍」那种事故，因此它带已知答案格子（含 -1/+1 边界）。

    `dither=True` 时在取整前加一份 TPDF（两个独立 1 LSB 均匀抖动相减 ⇒ 三角密度，
    峰峰 2 LSB）：本机默认端点的独占清单最深只有 16 位（tools/exclusive_probe.20261020-002924.json），
    而库里 24 位 FLAC 是大头（70 首抽样里 43 首是 24 位），所以**量化这条路天天会走到**。
    不加抖动的 `np.rint` 让量化误差和信号相关——淡出尾巴上那种「沙沙的颗粒」就是它。
    默认关着是刻意的：**bit-perfect 路径上抖动等于自己往信号里加东西**，那条路一个字节都不许动。
    所以这个开关由调用方（`_negotiate_exclusive` 算出的 `info["dither"]`）决定，而不是这里默认开。
    传 `rng`（任何有 `.random(n)` 的对象）只为测试可复现；生产不传就用系统随机源。
    """
    import numpy as np
    if container == 32 and bits == 32:
        return np.ascontiguousarray(out, dtype="<f4").tobytes()
    if container not in (16, 32) or bits > container:
        raise ValueError("不支持的容器位／有效位组合：%d in %d" % (bits, container))
    scale = float(1 << (bits - 1))
    x = np.clip(np.asarray(out, dtype=np.float64), -1.0, 1.0)
    v = x * scale
    if dither:
        g = rng if rng is not None else np.random.default_rng()
        # 形状跟着 v 走（v 是 帧×声道 的二维）：写 .size 拿一维向量去加二维数组，
        # 广播要么报错、要么在对齐的场合悄悄只抖到第一行——这一条是自测抓出来的。
        r1 = np.asarray(g.random(v.shape), dtype=np.float64)
        r2 = np.asarray(g.random(v.shape), dtype=np.float64)
        v = v + (r1 - r2)
    # 整型范围是**不对称**的：正半边只有 scale−1。少这一步 +1.0 会先算出 32768，
    # 再在 astype('<i2') 里绕回 −32768 —— 那不是削顶，是把满刻度正弦反了相，听感是硬爆音。
    q = np.clip(np.rint(v), -scale, scale - 1.0)
    if container == 16:
        return q.astype("<i2").tobytes()
    if container == 32:
        shift = container - bits
        return (q.astype("<i4") << np.int32(shift)).tobytes()
    raise ValueError("不支持的容器位：%d" % container)


# 事件对象要的访问权：CreateEventExW 的第 3 参是 flags、**第 4 参才是 dwDesiredAccess**。
# 早先这里第 4 参递了 0——事件建出来了，但当前线程拿它没有任何权限，
# WaitForSingleObject 立刻 WAIT_FAILED(0xFFFFFFFF)，独占的写循环就成了空转。
EVENT_ALL_ACCESS = 0x1F0001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF

# --------------------------------------------------------------------- 独占开流
class ExclusiveStream:
    """独占＋事件驱动的写循环。只在用户按了「独占输出」之后才由 audioplay 建。

    生命周期：open() → start() → 反复 write() → close()。
    任何一步失败都抛 WasapiError（上层负责回落共享并在回执里点名为什么回落）。
    """

    def __init__(self, device_id: str, wf: WAVEFORMATEXTENSIBLE,
                 buffer_frames: int, period_hns: int):
        self.device_id = device_id
        self.wf = wf
        self.requested_buffer_frames = buffer_frames
        self.period_hns = period_hns
        self.ac: Optional[AudioClient] = None
        self.rc = 0
        self.event = 0
        self.buffer_frames = 0
        self.period_frames = 0
        self.started = False
        self.written = 0
        self.blocks = 0
        self.silence_blocks = 0
        self.waits = 0
        self.wait_failed = 0

    def open(self) -> Dict[str, Any]:
        com_init(mta=True)
        self.ac = AudioClient(self.device_id)
        # 独占＋事件驱动：**缓冲时长必须等于设备包长**。这不是保守选择，是驱动给的原话——
        # 2026-10-20 00:28 本机默认端点「扬声器 (EDIFIER N500)」上以 (400000, 100000) 调
        # Initialize，返回 AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL(0x88890013)，一个字节都没进去。
        # 所以这里两个 REFERENCE_TIME 递**同一个变量**：写开成两个数（哪怕其中一个是另一个的
        # 整数倍）都会撞那条错误，而自测格子会把「两个数不同」直接判红。
        packet = self.period_hns
        hr = to_hr(_call(self.ac.p, "ac.initialize", ctypes.c_long,
                         [ctypes.c_int, ctypes.c_uint, ctypes.c_longlong, ctypes.c_longlong,
                          ctypes.c_void_p, ctypes.c_void_p],
                         [AUDCLNT_SHAREMODE_EXCLUSIVE,
                          AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                          packet, packet, ctypes.byref(self.wf), None]))
        if hr != S_OK:
            raise WasapiError("Initialize(EXCLUSIVE) 失败：%s（packet=%d）"
                              % (classify_hr(hr), packet), hr)
        _, kr = _libs()
        self.event = kr.CreateEventExW(None, None, 0, EVENT_ALL_ACCESS)
        if not self.event:
            raise WasapiError("CreateEventExW 失败（gle=%d）" % ctypes.get_last_error())
        hr = to_hr(_call(self.ac.p, "ac.set_event_handle", ctypes.c_long,
                         [ctypes.c_void_p], [self.event]))
        if hr != S_OK:
            raise WasapiError("SetEventHandle 失败：%s" % classify_hr(hr), hr)
        svc = guid_from(IID_IAUDIORENDERCLIENT)
        pp = ctypes.c_void_p(0)
        hr = to_hr(_call(self.ac.p, "ac.get_service", ctypes.c_long,
                         [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                         [ctypes.byref(svc), ctypes.byref(pp)]))
        if hr != S_OK:
            raise WasapiError("GetService(IAudioRenderClient) 失败：%s" % classify_hr(hr), hr)
        self.rc = pp.value
        n = ctypes.c_uint(0)
        hr = to_hr(_call(self.ac.p, "ac.get_buffer_size", ctypes.c_long,
                         [ctypes.POINTER(ctypes.c_uint)], [ctypes.byref(n)]))
        if hr != S_OK:
            raise WasapiError("GetBufferSize 失败：%s" % classify_hr(hr), hr)
        self.buffer_frames = n.value
        # 一周期多少帧：既然 Initialize 时包长＝缓冲时长（见上），GetBufferSize 报的就是
        # **一个包能装几帧**，这是驱动自己的数，比我按 100ns 反推的更权威（反推要取整，
        # 44100 的 10ms 就不是整帧）。所以：驱动给了就用驱动的，没给才回落到算的。
        calc = block_frames(self.period_hns, int(self.wf.Format.nSamplesPerSec))
        self.period_frames = int(self.buffer_frames) if self.buffer_frames else calc
        rec = self.status()
        rec["period_frames_calc"] = calc
        return rec

    def status(self) -> Dict[str, Any]:
        return {"mode": "exclusive", "started": self.started,
                "device": self.device_id, "format": wf_name(self.wf),
                "rate": int(self.wf.Format.nSamplesPerSec),
                "channels": int(self.wf.Format.nChannels),
                "valid_bits": int(self.wf.wValidBitsPerSample),
                "container_bits": int(self.wf.Format.wBitsPerSample),
                "buffer_frames": self.buffer_frames,
                "period_frames": self.period_frames,
                "period_ms": round(self.period_hns / 10000.0, 3),
                "written_frames": self.written, "blocks": self.blocks,
                "silence_blocks": self.silence_blocks,
                "waits": self.waits, "wait_failed": self.wait_failed}

    def padding(self) -> int:
        p = ctypes.c_uint(0)
        _call(self.ac.p, "ac.get_current_padding", ctypes.c_long,
              [ctypes.POINTER(ctypes.c_uint)], [ctypes.byref(p)])
        return p.value

    def _fill(self, frames: int, data: Optional[bytes]) -> None:
        pp = ctypes.c_void_p(0)
        hr = to_hr(_call(self.rc, "rc.get_buffer", ctypes.c_long,
                         [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                         [frames, ctypes.byref(pp)]))
        if hr != S_OK:
            raise WasapiError("GetBuffer(%d) 失败：%s" % (frames, classify_hr(hr)), hr)
        if data is not None:
            need = frames * self.wf.Format.nBlockAlign
            if len(data) < need:
                raise WasapiError("喂进独占缓冲的字节不够：%d < %d" % (len(data), need))
            ctypes.memmove(pp, data, need)
        flags = 0 if data is not None else 0x02   # AUDCLNT_BUFFERFLAGS_SILENT
        hr = to_hr(_call(self.rc, "rc.release_buffer", ctypes.c_long,
                         [ctypes.c_uint, ctypes.c_uint], [frames, flags]))
        if hr != S_OK:
            raise WasapiError("ReleaseBuffer(%d) 失败：%s" % (frames, classify_hr(hr)), hr)

    def prime_silence(self) -> None:
        self._fill(self.buffer_frames, None)

    def start(self) -> None:
        hr = to_hr(_call(self.ac.p, "ac.start", ctypes.c_long, [], []))
        if hr != S_OK:
            raise WasapiError("Start 失败：%s" % classify_hr(hr), hr)
        self.started = True

    def wait(self, ms: int = 100) -> int:
        """等一个设备事件。回 WaitForSingleObject 的原值（0/0x102/0xFFFFFFFF），
        调用方要能分清「到期没事件」和「句柄没权限」——后一种是量具失灵，不是慢。"""
        _, kr = _libs()
        r = kr.WaitForSingleObject(ctypes.c_void_p(self.event), ms)
        self.waits += 1
        if r == WAIT_FAILED:
            self.wait_failed += 1
        return r

    def write(self, data: bytes) -> int:
        """投**一个设备周期**（period_frames 帧）。data 不足时补零——绝不投半截：
        投得比周期少，等于让驱动在事件到点时手里没数，那一段是真 glitch，
        而补零只是如实的静音（上层把它记进 silence_blocks，进度不受影响）。
        返回实投帧数（= period_frames）。"""
        need = self.period_frames * self.wf.Format.nBlockAlign
        if len(data) < need:
            data = data + bytes(need - len(data))
            self.silence_blocks += 1
        self._fill(self.period_frames, data[:need])
        self.written += self.period_frames
        self.blocks += 1
        return self.period_frames

    def write_silence(self) -> int:
        """没货也要把这个周期占住：独占的事件节拍由设备驱动，跳过一次投递＝下一拍照样来，
        但驱动手里是空的。记进 silence_blocks，上层用它区分「静音填充」和「真丢帧」。"""
        self._fill(self.period_frames, None)
        self.silence_blocks += 1
        self.blocks += 1
        self.written += self.period_frames
        return self.period_frames

    def stop(self) -> None:
        if self.ac and self.started:
            try:
                _call(self.ac.p, "ac.stop", ctypes.c_long, [], [])
            except Exception:
                pass
            self.started = False

    def close(self) -> None:
        self.stop()
        if self.rc:
            _release(self.rc)
            self.rc = 0
        if self.event:
            try:
                _, kr = _libs()
                kr.CloseHandle(ctypes.c_void_p(self.event))
            except Exception:
                pass
            self.event = 0
        if self.ac:
            self.ac.close()
            self.ac = None


# ----------------------------------------------------------------------- 能力探针
def probe_device(device_id: str, channels: int = 2) -> Dict[str, Any]:
    """**只查不开**：mix 格式、设备周期、以及一整张候选独占格式的支持表。"""
    with AudioClient(device_id) as ac:
        try:
            mix = ac.mix_format()
        except WasapiError as e:
            return {"device": device_id, "error": str(e)}
        try:
            defperiod, minperiod = ac.device_period()
        except WasapiError as e:
            defperiod = minperiod = 0
        rows: List[Dict[str, Any]] = []
        for tag, wf in candidates_for(channels):
            try:
                hr, closest = ac.is_format_supported(AUDCLNT_SHAREMODE_EXCLUSIVE, wf)
            except WasapiError as e:
                rows.append({"tag": tag, "hr": None, "err": str(e)})
                continue
            rows.append({"tag": tag, "rate": int(wf.Format.nSamplesPerSec),
                         "valid_bits": int(wf.wValidBitsPerSample),
                         "container_bits": int(wf.Format.wBitsPerSample),
                         "hr": to_hr(hr), "hr_name": classify_hr(hr),
                         "closest": wf_name(closest) if closest else None})
        # 假阳性对照：一个物理上不存在的采样率。若它回了 UNSUPPORTED_FORMAT，
        # 说明「0x88890008 = AUDCLNT_E_UNSUPPORTED_FORMAT」这个**推出来的数值**在这台
        # 机器上被运行时认了一次（不是只有我在常量表里自说自话）。
        bogus_hr = None
        try:
            bogus_hr = to_hr(ac.is_format_supported(
                AUDCLNT_SHAREMODE_EXCLUSIVE,
                make_wf(1234, channels, 32, KSDATAFORMAT_SUBTYPE_IEEE_FLOAT, 32))[0])
        except Exception:
            pass
        return {"device": device_id,
                "mix": (wf_name(mix), int(mix.Format.nSamplesPerSec),
                        int(mix.wValidBitsPerSample), int(mix.Format.wBitsPerSample),
                        int(mix.Format.nChannels)) if mix else None,
                "default_period_hns": defperiod, "min_period_hns": minperiod,
                "default_period_ms": round(defperiod / 10000.0, 3),
                "exclusive_supported": [r for r in rows if r.get("hr") == S_OK],
                "table": rows,
                "bogus_rate_hr": bogus_hr,
                "bogus_is_unsupported_format": bogus_hr == AUDCLNT_E_UNSUPPORTED_FORMAT}


def probe_all() -> Dict[str, Any]:
    ids = device_ids()
    out = {"devices": [], "note": "探针不调 Initialize：这一段不占有任何设备、不出声"}
    for d in ids:
        out["devices"].append(probe_device(d["id"]))
    out["count"] = len(ids)
    return out


def _pa_render_names() -> Dict[str, Any]:
    """第二来源：pyaudiowpatch（PortAudio 自己那套 WASAPI 枚举）里的渲染端点名。
    拿它做**独立对照**：我这条 COM 路径和它是两份互不相干的实现，名字对得上，
    「pid=14＝友好名」才不是我自己读自己。缺库/出错时如实回 unavailable，不静默跳过
    （静默跳过＝把「没量」写成「过了」）。"""
    try:
        import pyaudiowpatch as pa
    except Exception as e:
        return {"status": "unavailable", "why": "%s: %s" % (type(e).__name__, e)}
    try:
        p = pa.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            # 键名按本机实测：这份 dict 的键是 index/deviceCount/…（**没有** deviceIndex），
            # 设备 dict 里有 isLoopbackDevice ⇒ 对照要排除 loopback 那批（它们不是渲染端点）
            base = int(wasapi["index"])
            first = None
            for i in range(p.get_device_count()):
                if int(p.get_device_info_by_index(i)["hostApi"]) == base:
                    first = i
                    break
            if first is None:
                return {"status": "error", "why": "WASAPI hostApi %d 下没有设备" % base}
            names = []
            for i in range(first, p.get_device_count()):
                di = p.get_device_info_by_index(i)
                if int(di["hostApi"]) != base:
                    break
                if int(di["maxOutputChannels"]) > 0 and not di.get("isLoopbackDevice"):
                    names.append(di["name"])
            return {"status": "ok", "names": names, "host_api": base}
        finally:
            p.terminate()
    except Exception as e:
        return {"status": "error", "why": "%s: %s" % (type(e).__name__, e)}


def names_all() -> Dict[str, Any]:
    """把每个渲染端点的属性存储**整表枚举**出来，专门回答两件事：
    ① OpenPropertyStore 在这台机器上要递两参还是三参（两形态各跑一行）；
    ② 友好名到底落在哪个 (fmtid, pid)——不拿文档里借来的 14 当结论，
       而是让「哪一号键装着扬声器那行字」由本机说一遍，再和 pyaudiowpatch 的名字对多重集。
    全程只读属性，不 Initialize ⇒ 不占有设备、不出声，用户在场也能跑。"""
    out: Dict[str, Any] = {"devices": [], "note": "只读 PropertyStore，不占有设备"}
    for d in device_ids():
        dev = _device_ptr(d["id"])
        try:
            props = device_props(dev)
            strs = [{"fmtid": it.get("fmtid"), "pid": it.get("pid"), "str": it.get("str")}
                    for it in props.get("items", [])
                    if it.get("vt") == VT_LPWSTR and it.get("str")]
            out["devices"].append({"index": d["index"], "id": d["id"], "name": d["name"],
                                   "name_diag": d.get("name_diag"),
                                   "open_hr": props.get("open_hr"),
                                   "count": props.get("count"),
                                   "count_hr": props.get("count_hr"),
                                   "strings": strs})
        finally:
            _release(dev)
    out["count"] = len(out["devices"])
    # ② 的那个「再对多重集」在这里落账：两份互不相干的实现（我手写的 COM 虚表 vs PortAudio）
    #   读出来的名字必须一致；不一致就是「我读错了键」而不是「系统真有两个名字」。
    out["cross_source"] = _names_cross_check(out["devices"], _pa_render_names())
    return out


def _names_cross_check(devices: List[Dict[str, Any]], pa_res: Dict[str, Any]) -> Dict[str, Any]:
    mine = sorted(d["name"] for d in devices if d.get("name"))
    if pa_res.get("status") != "ok":
        # 没对照源就如实写 unavailable：不许让这一格在缺库时显示成「过了」
        return {"status": pa_res["status"], "why": pa_res.get("why"),
                "com_names": mine, "agree": None}
    theirs = sorted(n for n in pa_res["names"] if "[Loopback]" not in n)
    return {"status": "ok", "com_names": mine, "pa_names": theirs,
            "agree": mine == theirs,
            "only_com": [n for n in mine if n not in theirs],
            "only_pa": [n for n in theirs if n not in mine],
            "com_missing_name": [d["index"] for d in devices if not d.get("name")]}


# ------------------------------------------------------------------------ 已知答案
def _selftest() -> int:
    import numpy as np
    res: List[Tuple[str, bool, Any]] = []

    def ck(name, cond, detail=None):
        try:
            ok = bool(cond)
        except BaseException as e:      # 断言自己不许崩（记忆里那条老规矩）
            res.append((name, False, "CRASH %r" % e))
            return
        res.append((name, ok, detail))

    # 1) GUID：Data4 是 8 个单字节，不是 big-endian u64
    g = guid_from(CLSID_MMDEVICE_ENUMERATOR)
    ck("GUID 解析回读等价", g.as_str() == CLSID_MMDEVICE_ENUMERATOR, g.as_str())
    ck("GUID Data4 逐字节（前两字节 0x8e 0x3d，不是整段大端 u64）",
       bytes(g.Data4)[:2] == bytes([0x8e, 0x3d]) and g.Data4[0] == 0x8e
       and bytes(g.Data4) == bytes.fromhex("8e3dc4579291692e"), bytes(g.Data4).hex())
    ck("GUID 大写/花括号都能吃",
       guid_from("{BCDE0395-E52F-467C-8E3D-C4579291692E}").as_str() == CLSID_MMDEVICE_ENUMERATOR)
    bad = 0
    try:
        guid_from("deadbeef")
    except ValueError:
        bad = 1
    ck("GUID 长度不对要报错而不是造个错的出来", bad == 1)

    # 2) HRESULT 推导式：AUDCLNT_ERR(n) 的几个已知值
    ck("AUDCLNT 错误码：推导式复现 ac.h:1844-1874 逐行的 n（低 16 位＝n，facility＝0x889）",
       AUDCLNT_E_UNSUPPORTED_FORMAT == 0x88890008              # ac.h:1851  n=0x8
       and AUDCLNT_E_DEVICE_IN_USE == 0x8889000A               # ac.h:1853  n=0xa
       and AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED == 0x8889000E  # ac.h:1856  n=0xe
       and AUDCLNT_E_RESOURCES_INVALIDATED == 0x88890026       # ac.h:1874  n=0x26
       and AUDCLNT_E_DEVICE_INVALIDATED == 0x88890004,         # ac.h:1847  n=0x4
       [hex(AUDCLNT_E_UNSUPPORTED_FORMAT), hex(AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED)])
    ck("facility 字段占 bit16–27（12 位，掩码 0xFFF）——这里连写错过两档掩码"
       "（0x1FF、0x3FF 都得到 0x89），格子两次翻红才把位宽钉下来",
       ((AUDCLNT_E_UNSUPPORTED_FORMAT >> 16) & 0xFFF) == 0x889
       and (AUDCLNT_E_UNSUPPORTED_FORMAT & 0xFFFF) == 0x0008
       and (AUDCLNT_E_UNSUPPORTED_FORMAT >> 31) == 1,
       hex((AUDCLNT_E_UNSUPPORTED_FORMAT >> 16) & 0xFFF))
    ck("0x88890026 是 RESOURCES_INVALIDATED，**不是** EXCLUSIVE_MODE_NOT_ALLOWED"
       "（这一条钉住一次真实手抄错：早先台账把 0x26 记给了独占那条）",
       AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED != 0x88890026
       and AUDCLNT_E_RESOURCES_INVALIDATED == 0x88890026,
       [hex(AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED), hex(AUDCLNT_E_RESOURCES_INVALIDATED)])
    ck("to_hr 把 ctypes 的负数收成无符号（E_NOINTERFACE 的有符号形态）",
       to_hr(-2147467262) == E_NOINTERFACE and to_hr(-1) == 0xFFFFFFFF,
       [hex(to_hr(-2147467262)), hex(to_hr(-1))])
    ck("classify_hr 认得具名码/不认得时给十六进制",
       classify_hr(0x88890008) == "AUDCLNT_E_UNSUPPORTED_FORMAT"
       and classify_hr(0x1234) == "0x00001234")

    # 3) 结构体字节布局：尺寸与偏移是驱动读的东西，错了不会报错会静音/杂音
    ck("WAVEFORMATEX 尺寸 18（mmreg.h pack(1)；ctypes 默认会补成 20 ⇒ 驱动读错位）",
       ctypes.sizeof(WAVEFORMATEX) == 18, ctypes.sizeof(WAVEFORMATEX))
    ck("WAVEFORMATEX 七个字段偏移逐格对原文",
       [_off(WAVEFORMATEX, f) for f in
        ("wFormatTag", "nChannels", "nSamplesPerSec", "nAvgBytesPerSec",
         "nBlockAlign", "wBitsPerSample", "cbSize")] == [0, 2, 4, 8, 12, 14, 16],
       [_off(WAVEFORMATEX, f) for f in ("nSamplesPerSec", "nAvgBytesPerSec", "cbSize")])
    ck("WAVEFORMATEXTENSIBLE 尺寸 40（cbSize 应为 22）",
       ctypes.sizeof(WAVEFORMATEXTENSIBLE) == 40
       and make_wf(48000).Format.cbSize == 22,
       ctypes.sizeof(WAVEFORMATEXTENSIBLE))
    ck("WAVEFORMATEXTENSIBLE 四个成员偏移＝0/18/20/24",
       [_off(WAVEFORMATEXTENSIBLE, f) for f in
        ("Format", "wValidBitsPerSample", "dwChannelMask", "SubFormat")] == [0, 18, 20, 24],
       [_off(WAVEFORMATEXTENSIBLE, f) for f in ("wValidBitsPerSample", "dwChannelMask", "SubFormat")])
    # PROPERTYKEY / PROPVARIANT：propidl.h:224-309。这一格的代价不是「读不到名字」，
    # 是**让 COM 往我的缓冲区后面写**——早先声明到 pv 就收尾（16 字节），
    # 而值联合里有 16 字节的成员（CALPWSTR 那批 ULONG+指针），整条结构 x64 上是 24。
    ck("PROPERTYKEY 尺寸 20、pid 偏移 16（wtypes.h:573-576 GUID+DWORD）",
       ctypes.sizeof(PROPERTYKEY) == 20 and _off(PROPERTYKEY, "pid") == 16
       and _off(PROPERTYKEY, "fmtid") == 0,
       [ctypes.sizeof(PROPERTYKEY), _off(PROPERTYKEY, "pid")])
    ck("PROPVARIANT：vt@0、值联合体@8（propidl.h 三个 WORD pad 之后紧接着就是联合体）",
       _off(PROPVARIANT, "vt") == 0 and _off(PROPVARIANT, "pv") == 8
       and [_off(PROPVARIANT, f) for f in ("r1", "r2", "r3")] == [2, 4, 6],
       [_off(PROPVARIANT, f) for f in ("vt", "r1", "r2", "r3", "pv")])
    ck("PROPVARIANT 尺寸 24＝8(头)＋16(值联合最宽成员)；缓冲区不足 24 就是越界写",
       ctypes.sizeof(PROPVARIANT) == 24, ctypes.sizeof(PROPVARIANT))
    ck("PROPVARIANT 的 tail 落在 16：留出值联合后半（这条就是当年少写的那 8 字节）",
       _off(PROPVARIANT, "tail") == 16, _off(PROPVARIANT, "tail"))
    ck("GUID 结构在 pack 环境里仍是 16 字节（PROPERTYKEY 的 fmtid 宽度和它一致）",
       ctypes.sizeof(GUID) == 16 and ctypes.sizeof(PROPERTYKEY) == 16 + 4,
       ctypes.sizeof(GUID))
    wf = make_wf(96000, 2, 24, KSDATAFORMAT_SUBTYPE_PCM, 32)
    ck("24-in-32：blockAlign 按**容器**算（=8，不是按有效位算的 6）",
       wf.Format.nBlockAlign == 8 and wf.wValidBitsPerSample == 24
       and wf.Format.wBitsPerSample == 32, wf.Format.nBlockAlign)
    ck("avgBytesPerSec = rate*blockAlign（算错＝驱动说格式不合）",
       wf.Format.nAvgBytesPerSec == 96000 * 8)
    ck("f32 的 SubFormat 是 IEEE_FLOAT 而不是 PCM",
       make_wf(48000).SubFormat.as_str() == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)
    ck("wf_name 三种形状各说各话",
       wf_name(make_wf(48000)) == "f32@48000/2h"
       and wf_name(make_wf(44100, 2, 16, KSDATAFORMAT_SUBTYPE_PCM, 16)) == "pcm16@44100/2h"
       and wf_name(wf) == "pcm24@96000/2h",
       [wf_name(make_wf(48000)), wf_name(make_wf(44100, 2, 16, KSDATAFORMAT_SUBTYPE_PCM, 16)),
        wf_name(wf)])
    try:
        make_wf(48000, 2, 32, KSDATAFORMAT_SUBTYPE_PCM, 16)
        ck("容器装不下有效位要拒绝", False)
    except ValueError:
        ck("容器装不下有效位要拒绝", True)

    # 4) read_wf：cbSize 两种形状都要能吃，且非 extensible 要按 tag 反推子格式
    raw = WAVEFORMATEXTENSIBLE()
    raw.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE
    raw.Format.nChannels = 2
    raw.Format.nSamplesPerSec = 48000
    raw.Format.nBlockAlign = 4
    raw.Format.wBitsPerSample = 32
    raw.Format.nAvgBytesPerSec = 192000
    raw.Format.cbSize = 22
    raw.wValidBitsPerSample = 24
    raw.SubFormat = guid_from(KSDATAFORMAT_SUBTYPE_PCM)
    ck("read_wf 吃 extensible（24 位有效）",
       read_wf(ctypes.addressof(raw)).wValidBitsPerSample == 24
       and read_wf(ctypes.addressof(raw)).SubFormat.as_str() == KSDATAFORMAT_SUBTYPE_PCM)
    plain = WAVEFORMATEX(WAVE_FORMAT_IEEE_FLOAT, 2, 44100, 352800, 8, 32, 0)
    got = read_wf(ctypes.addressof(plain))
    ck("read_wf 吃 cbSize=0 的旧布局时按 tag 反推（不是当 0 位有效）",
       got.Format.wBitsPerSample == 32 and got.wValidBitsPerSample == 32
       and got.SubFormat.as_str() == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT, wf_name(got))
    ck("read_wf(0) 回 None 不炸", read_wf(0) is None)

    # 5) 时间数学：向上取整（向下取整会让 Initialize 说周期不合）
    ck("hns_for 向上取整（44100/512 帧不是整数个 100ns）",
       hns_for(44100, 512) == 116100 and hns_for(48000, 480) == 100000,
       hns_for(44100, 512))
    ck("block_frames：整除那档不多算一帧（10ms@44100＝441；上一版期望写的是 442，那是抄错）",
       block_frames(100000, 44100) == 441 and block_frames(100000, 48000) == 480,
       [block_frames(100000, 44100), block_frames(100000, 48000)])
    ck("block_frames：除不尽时向上（5ms@44100＝220.5→221；向下就少半帧＝周期性 underrun）",
       block_frames(50000, 44100) == 221, block_frames(50000, 44100))
    ck("往返不变式：先帧→hns 再 hns→帧，拿到的帧数**只多不少**（少了就是写缓冲欠裁）",
       all(block_frames(hns_for(r, n), r) >= n
           for r in (44100, 48000, 96000, 176400) for n in range(1, 513)),
       [(r, n) for r in (44100, 48000, 96000, 176400) for n in range(1, 513)
        if block_frames(hns_for(r, n), r) < n][:4])
    ck("hns_for 对 0 采样率/负帧数要拒绝",
       _raises(lambda: hns_for(0, 10)) and _raises(lambda: hns_for(48000, -1)))

    # 6) frames_to_device：三条编码路径的已知答案（含满刻度与 -1）
    x = np.array([[0.0, 0.0], [1.0, -1.0], [0.5, -0.5]], np.float32)
    b16 = frames_to_device(x, 16, 16)
    ck("s16：满刻度=32767、负满刻度=−32768、小端字节序",
       struct.unpack("<6h", b16) == (0, 0, 32767, -32768, 16384, -16384),
       struct.unpack("<6h", b16))
    b24 = frames_to_device(x, 24, 32)
    v = struct.unpack("<6i", b24)
    ck("24-in-32：有效位左移 (32−24)，正满刻度＝8388607<<8",
       v[2] == (8388607 << 8) and v[3] == (-8388608 << 8) and v[0] == 0, v)
    b32f = frames_to_device(x, 32, 32)
    ck("f32→f32 是逐字节不动（这条就是 bit-perfect 的那条）",
       b32f == np.ascontiguousarray(x, "<f4").tobytes())
    ck("越界样本先削顶再量化（不许绕回成反向的爆音）",
       struct.unpack("<2h", frames_to_device(np.array([[2.0, -3.0]], np.float32), 16, 16))
       == (32767, -32768))
    # 6b) 抖动只碰「要量化」那条路，且碰得有据可查
    ex = np.zeros((4000, 2), np.float32)
    dg = np.random.default_rng(20261020)
    dz = struct.unpack("<%dh" % (ex.size,), frames_to_device(ex, 16, 16, dither=True, rng=dg))
    n1 = sum(1 for i in dz if abs(i) == 1)
    nz = sum(1 for i in dz if i == 0)
    ck("TPDF 抖动：静默上只会 ±1（峰峰 2 LSB 的三角抖动取整后不可能跑出 ±1）",
       max(abs(i) for i in dz) <= 1, max(abs(i) for i in dz))
    ck("TPDF 抖动：形状对得上理论值（P(|n|>0.5)＝25%，实算 0 占 75%）",
       0.68 < nz / len(dz) < 0.82 and 0.18 < n1 / len(dz) < 0.32,
       "零=%d 非零=%d 共=%d" % (nz, n1, len(dz)))
    ck("TPDF 抖动：正负对称（偏置＝在静默上直流偏移，那是缺陷不是抖动）",
       abs(sum(1 for i in dz if i > 0) - sum(1 for i in dz if i < 0)) <= 12,
       "%+d" % (sum(1 for i in dz if i > 0) - sum(1 for i in dz if i < 0)))
    off = np.array([[0.3, -0.7]], np.float32)
    ck("抖一格最多挪 1 LSB（不许把「量化」写成「乱写」）",
       all(abs(a - b) <= 1 for a, b in zip(
           struct.unpack("<2h", frames_to_device(off, 16, 16)),
           struct.unpack("<2h", frames_to_device(off, 16, 16, dither=True,
                                                 rng=np.random.default_rng(7))))),
       struct.unpack("<2h", frames_to_device(off, 16, 16, dither=True,
                                            rng=np.random.default_rng(7))))
    ck("关掉抖动 ⇒ 逐字节和精确量化相同（bit-perfect 路径不该被偷偷加料）",
       frames_to_device(x, 16, 16, dither=False) == b16)
    ck("f32 那条直通路无视 dither（设备给浮点时抖动等于无中生有）",
       frames_to_device(x, 32, 32, dither=True) == b32f)
    ck("削顶在抖动之后仍然成立（+1 LSB 抖动不能把满刻度顶出容器范围）",
       all(abs(i) <= 32767 for i in struct.unpack(
           "<%dh" % (2000 * 2,),
           frames_to_device(np.ones((2000, 2), np.float32), 16, 16, dither=True,
                            rng=np.random.default_rng(3)))))
    try:
        frames_to_device(x, 20, 24)
        ck("不认识的容器位要拒绝", False)
    except ValueError:
        ck("不认识的容器位要拒绝", True)

    # 7) pick_exclusive_format：四条出口各一条，外加「一个都不支持」必须回 shared
    def s(rate, bits, cont, hr=S_OK, tag="t"):
        return {"rate": rate, "valid_bits": bits, "container_bits": cont, "hr": hr, "tag": tag}
    d = pick_exclusive_format([s(48000, 24, 32), s(44100, 16, 16)], 44100, 48000, 16)
    ck("有同率且位深够的 ⇒ 选它（说 bit-perfect）",
       d["mode"] == "exclusive" and d["rate"] == 44100 and "不重采样" in d["why"]
       and d["bit_perfect"] is True and d["resampled"] is False, d)
    d = pick_exclusive_format([s(48000, 24, 32), s(96000, 32, 32)], 44100, 48000, 24)
    ck("没有同率 ⇒ 退到目标率并**明说还要重采样**（不许冒充 bit-perfect）",
       d["mode"] == "exclusive" and d["rate"] == 48000 and "仍要重采样" in d["why"]
       and d["bit_perfect"] is False and d["resampled"] is True, d)
    # 本机真形状（exclusive_probe.20261020-002924.json：独占清单只有 44100/48000/96000 的
    # 16 位三档）。源按 24 位处理时它落的是**第三家**（同率但浅），文案必须说「量化」而不是
    # 「没有与源同率的格式、仍要重采样」—— 那台设备明明同率，说重采样就是界面在撒谎。
    host = [s(44100, 16, 16, tag="44100-pcm16"), s(48000, 16, 16, tag="48000-pcm16"),
            s(96000, 16, 16, tag="96000-pcm16")]
    d = pick_exclusive_format(host, 44100, 44100, 24)
    ck("同率但比源浅 ⇒ 选同率那档，明说「量化到 16 位」，且 bit_perfect=False",
       d["rate"] == 44100 and d["bits"] == 16 and d["bit_perfect"] is False
       and d["resampled"] is False and "量化" in d["why"], d)
    # 反着核一句：这一家**不许**说「仍要重采样」或「没有与源同率的格式」——那台设备明明同率。
    # （判"不含某串"要用整句而不是子串：写 "重采样" not in why 会被"不重采样"自己撞红。）
    ck("同率但比源浅这一家不许谎称「没有同率格式／仍要重采样」",
       "仍要重采样" not in d["why"] and "没有与源同率" not in d["why"], d["why"])
    d = pick_exclusive_format(host, 44100, 44100, 16)
    ck("同一家表、源位深改成 16 ⇒ 落第一家（bit_perfect=True）：位深假设单独就能翻面，"
       "所以它必须是设置里一个看得见的数，不是代码里写死的",
       d["bit_perfect"] is True and d["rate"] == 44100, d)
    d = pick_exclusive_format([s(48000, 16, 16), s(48000, 24, 32)], 48000, 48000, 24)
    ck("位深不够的候选让给够的（24 位源不许退到 16 位独占）",
       d["bits"] == 24, d)
    # 「够用的最浅档」vs「不知道就往深里赌」这两条方向相反，所以各要一条已知答案：
    # 只留一条的话，另一条方向被写反了也没人拦（把 depth 的符号抹掉，两条里必有一条翻红）。
    d = pick_exclusive_format([s(44100, 24, 32, tag="44100-pcm24"), s(44100, 16, 16,
                                                                   tag="44100-pcm16")],
                              44100, 96000, 16)
    ck("源 16 位、同率有 16 和 24 两档 ⇒ 选 16（够用且最省，不必塞进 24-in-32）",
       d["bit_perfect"] is True and d["bits"] == 16 and d["container"] == 16, d)
    d = pick_exclusive_format([s(44100, 16, 16, tag="44100-pcm16"), s(44100, 24, 32,
                                                                      tag="44100-pcm24")],
                              44100, 96000, 0)
    ck("源位深未知（0）⇒ 往深里选 24，别在开流这一步先把万一的 24 位源削掉",
       d["bits"] == 24 and d["rate"] == 44100, d)
    d = pick_exclusive_format([s(48000, 24, 32, AUDCLNT_E_UNSUPPORTED_FORMAT)], 48000, 48000)
    ck("全不支持 ⇒ mode=shared 且给出原因（fail-closed，不假装独占）",
       d["mode"] == "shared" and d["why"] and d["bit_perfect"] is False, d)
    d = pick_exclusive_format([], 48000, 48000)
    ck("一个都没探到 ⇒ 也走 shared（空表不等于成功）", d["mode"] == "shared")
    d = pick_exclusive_format([s(48000, 32, 32), s(44100, 24, 32), s(44100, 32, 32)],
                              44100, 96000, 24)
    ck("同率候选里再按「位深够→贴着目标率→率高」排序（形状固定的表也要选对）",
       d["rate"] == 44100 and d["bits"] == 24, d)

    # 8) 槽位表自身的完整性：_call 用不上的名字不许留在表里，用的必须都在表里
    ck("每个用到的槽名都在 _SLOT 里（写错名字是 KeyError 不是调错槽）",
       all(k in _SLOT for k in ("ac.initialize", "rc.release_buffer", "dev.activate",
                               "coll.get_count", "coll.item")))
    # 「槽位号只活在 _SLOT 一张表里」是这份文件的存在理由之一，所以它得是机器核的，
    # 不能只在 docstring 里承诺一次。取 _selftest 之前的那段（产品代码）扫裸偏移。
    import io as _io
    import re as _re
    _head = _io.open(__file__, encoding="utf-8").read().split("def _selftest")[0]
    _raw = [l.strip() for l in _head.splitlines() if _re.search(r"_slot\(", l)]
    ck("产品代码里 `_slot(` 只出现在定义处和 _call 里（没有第三处绕过表的调用）",
       len(_raw) == 2, _raw)
    ck("产品代码里**没有** `_slot(…, 3)` 这种裸偏移（槽号只能从 _SLOT 来）",
       not _re.findall(r"_slot\([^)]*,\s*\d", _head),
       _re.findall(r".*_slot\([^)]*,\s*\d.*", _head)[:3])
    ck("_SLOT 槽位号互不越界（IAudioClient 12 个方法 ⇒ 最大 14）",
       _SLOT["ac.get_service"] == 14 and _SLOT["rc.release_buffer"] == 4
       and _SLOT["dev.get_state"] == 6 and _SLOT["enum.get_device"] == 5)
    # 句柄/槽位那几条不是「文案格子」：报哪一句，就证明内存被动到了哪一层。
    # 反面写法是拿 0x1000 这类未映射地址去试——那不是异常，是访问违例，自测会把进程打死，
    # 于是「抓到缺陷」与「量具自己崩了」分不清（本项目在 rc=127 空 stderr 那条上吃过亏）。
    # 改成自己分配几块**可读**内存当假对象：三道闸各走一条，再补一条正向
    # ——只有正向成立，前几条「拦住」才不是无条件 raise。
    zero_vt = (ctypes.c_void_p * 16)()
    fn_ptr = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(lambda this: 0)
    fn_addr = ctypes.cast(fn_ptr, ctypes.c_void_p).value
    ok_vt = (ctypes.c_void_p * 16)(*[fn_addr] * 16)
    obj_vtbl_zero = ctypes.c_void_p(0)
    obj_slot_zero = ctypes.c_void_p(ctypes.addressof(zero_vt))
    obj_ok = ctypes.c_void_p(ctypes.addressof(ok_vt))
    m_handle = _msg(lambda: _slot(0, _SLOT["ac.start"]))
    m_vtbl = _msg(lambda: _slot(ctypes.addressof(obj_vtbl_zero), _SLOT["ac.start"]))
    m_slot = _msg(lambda: _slot(ctypes.addressof(obj_slot_zero), _SLOT["ac.get_mix_format"]))
    got_fn = _slot(ctypes.addressof(obj_ok), 0)
    ck("_slot(0,…) 被句柄闸门拦住，且报的是「句柄为空」不是「vtable 为空」"
       "（这条不是文案格子：报哪句就证明内存一次都没被解引用过）",
       "句柄为空" in m_handle and "vtable" not in m_handle
       and str(_SLOT["ac.start"]) in m_handle, m_handle)
    ck("this 非 0 但 vtable 指针是 0：第二道闸拦住（报 vtable、不去读槽）",
       "vtable 为空" in m_vtbl and "槽" not in m_vtbl, m_vtbl)
    ck("vtable 可读但那一槽是空指针：第三道闸拦住（槽号对得上）",
       "槽 %d 是空指针" % _SLOT["ac.get_mix_format"] in m_slot, m_slot)
    ck("槽位号写对了就原样回函数地址（正向：拿真函数的 vtable 验，全程不调用）",
       got_fn == fn_addr, [hex(got_fn), hex(fn_addr)])
    ck("_msg 在不抛异常时回**空串**（反向哨兵：没这条，上面三道「拦住」可以是恒绿的量具）",
       _msg(lambda: _slot(ctypes.addressof(obj_ok), 3)) == "",
       repr(_msg(lambda: _slot(ctypes.addressof(obj_ok), 3))))
    ck("_call 遇到表里没有的名字先 KeyError（写错名字不许悄悄调错槽）",
       "KeyError" in _msg(lambda: _call(0, "ac.starts", ctypes.c_int, [], [])),
       _msg(lambda: _call(0, "ac.starts", ctypes.c_int, [], [])))

    # 9) 独占写循环的两条真缺陷，用**假 IAudioRenderClient**当场量（不碰任何设备）：
    #    ① CreateEventExW 的第 4 参是 dwDesiredAccess——早先递 0，事件建成功但本线程没权限等它，
    #       WaitForSingleObject 立刻 WAIT_FAILED，写循环看着在跑其实空转（源码扫字节，不扫我的印象）；
    #    ② write() 投的必须是**整周期**，短了补零并如实记 silence_blocks。
    src = io.open(__file__, encoding="utf-8").read()
    ce = [ln.strip() for ln in src.splitlines() if "CreateEventExW(" in ln and "argtypes" not in ln]
    ck("CreateEventExW 的调用点把 EVENT_ALL_ACCESS 递在第 4 参（dwDesiredAccess）"
       "——第 4 参是 0 的写法会让独占写循环永远 WAIT_FAILED",
       len(ce) == 1 and "EVENT_ALL_ACCESS" in ce[0] and "None, None, 0, EVENT_ALL_ACCESS" in ce[0],
       ce)
    ini = [ln.strip() for ln in src.splitlines()
           if "AUDCLNT_STREAMFLAGS_EVENTCALLBACK," in ln
           or (ln.strip().startswith("packet, packet") )]
    ck("Initialize(EXCLUSIVE) 的两个 REFERENCE_TIME 是**同一个变量**（packet, packet）"
       "——本机实测不等就报 AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL",
       any("packet, packet" in ln for ln in src.splitlines())
       and "AUDCLNT_STREAMFLAGS_EVENTCALLBACK," in src,
       ini[:3])
    ck("WAIT_* 三个码钉在文档值上（0/258/0xFFFFFFFF）：混用「超时」和「句柄没权限」就靠这三个数",
       (WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED) == (0, 258, 0xFFFFFFFF),
       [WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED])
    sink: List[Tuple[int, int]] = []
    fake_buf = (ctypes.c_char * 65536)()
    gb = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
                          ctypes.POINTER(ctypes.c_void_p))(
        lambda this, frames, pp: (pp.__setitem__(0, ctypes.addressof(fake_buf)), 0)[1])
    rb = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint)(
        lambda this, frames, flags: (sink.append((frames, flags)), 0)[1])
    nul = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(lambda this: 0)
    vt = (ctypes.c_void_p * 8)(*[ctypes.cast(nul, ctypes.c_void_p).value] * 3
                               + [ctypes.cast(gb, ctypes.c_void_p).value,
                                  ctypes.cast(rb, ctypes.c_void_p).value] + [0, 0])
    # 假对象的形状：COM 对象**第一个字段是 vtable 指针**，不是 vtable 本身。
    # 直接把 addressof(vt) 当 this 递进去，_slot 会把「槽 0 的函数地址」当成 vtable 再解一层，
    # 读进代码段 ⇒ 访问违例（这一格第一次跑就是这么崩的，不是产品崩）。
    obj_rc = ctypes.c_void_p(ctypes.addressof(vt))
    ex = ExclusiveStream.__new__(ExclusiveStream)
    ex.wf = make_wf(44100, 2, 16, KSDATAFORMAT_SUBTYPE_PCM, 16)
    ex.period_hns = 100000                        # 10ms：本机 --probe 量到的 default_period_ms 就是 10.0
    ex.period_frames = block_frames(ex.period_hns, 44100)
    ex.buffer_frames = 4410
    ex.rc = ctypes.addressof(obj_rc)
    ex.ac = None
    ex.started = True
    ex.device_id = "fake"
    ex.written = ex.blocks = ex.silence_blocks = ex.waits = ex.wait_failed = 0
    ck("period_frames 由周期反推（10ms@44100＝441；10ms 就是本机 --probe 量到的 default_period）",
       ex.period_frames == 441, ex.period_frames)
    got = ex.write(b"\x01" * (10 * ex.wf.Format.nBlockAlign))     # 只喂 10 帧
    ba = ex.wf.Format.nBlockAlign
    ck("喂不满一个周期 ⇒ 投的仍是整周期（补零），并把这一块记进 silence_blocks",
       got == ex.period_frames and sink and sink[-1][0] == ex.period_frames
       and ex.silence_blocks == 1 and ex.blocks == 1, [sink[-1], ex.silence_blocks])
    ck("补进去的确实是零（假缓冲里第 10 帧之后是 0x00，不是上一块的残留）",
       bytes(fake_buf[:10 * ba]) == b"\x01" * (10 * ba)
       and bytes(fake_buf[10 * ba:ex.period_frames * ba]) == b"\x00" * ((ex.period_frames - 10) * ba))
    ck("ReleaseBuffer 的帧数＝GetBuffer 的帧数（少一位就是拿半截周期糊弄驱动）",
       all(f == ex.period_frames for f, _fl in sink), sink)
    ck("满周期投递**不**记 silence_blocks（正向哨兵：没这条，上一条可以是恒加一）",
       ex.write(b"\x02" * (ex.period_frames * ba)) == ex.period_frames
       and ex.silence_blocks == 1, ex.silence_blocks)
    ck("write_silence 走 SILENT 标志(0x02)且不谎报写了真数据",
       ex.write_silence() == ex.period_frames and sink[-1][1] == 0x02
       and ex.silence_blocks == 2, sink[-1])
    stt = ex.status()
    ck("status() 把周期、缓冲、静音块、等待失败都带出来（上层要拿这些判「在放」还是「在空转」）",
       all(k in stt for k in ("period_frames", "buffer_frames", "silence_blocks",
                              "wait_failed", "written_frames"))
       and stt["period_frames"] == ex.period_frames, sorted(stt))
    ex.rc = 0
    ex.started = False
    out = [r for r in res if not r[1]]
    print("\n".join("  [%s] %s%s" % ("PASS" if ok else "FAIL", n,
                                     "" if ok or d is None else "  <- %r" % (d,))
                    for n, ok, d in res))
    print("wasapi 已知答案：%d/%d" % (len(res) - len(out), len(res)))
    return 0 if not out else 1


def _off(cls, name: str) -> int:
    """ctypes 的字段偏移（自测里当「结构布局尺」用；拿不到就回 -1，让格子红而不是把量具打死）。"""
    try:
        return int(getattr(cls, name).offset)
    except BaseException:
        return -1


def _msg(fn) -> str:
    """跑一下并把异常收成一行「类型: 消息」；没抛异常就回**空串**。

    闸门那几条要判的是「哪一道闸拦的」，光一个 True 分不出层（三道闸都返 True 就等于没量）。
    带上类型名是因为 `str(KeyError('ac.starts'))` 只有 `'ac.starts'`，不写类型就没法判
    「名字写错时到底是 KeyError 还是调错槽之后的 RuntimeError」。
    """
    try:
        fn()
    except BaseException as e:
        return "%s: %s" % (type(e).__name__, e)
    return ""


def _raises(fn) -> bool:
    try:
        fn()
    except BaseException:
        return True
    return False


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--probe" in sys.argv:
        print(json.dumps(probe_all(), ensure_ascii=False, indent=1))
        sys.exit(0)
    if "--names" in sys.argv:
        print(json.dumps(names_all(), ensure_ascii=False, indent=1))
        sys.exit(0)
    print(__doc__)
    print("跑法：python -X utf8 -m sp.wasapi --selftest ｜ python -X utf8 -m sp.wasapi --probe")
    print("     --probe 只查能力，不调 Initialize ⇒ 不锁设备、不出声（锁屏/用户在场都能跑）")
    print("     --names 枚举每个端点的属性存储（同样只读，用来机器核友好名那个 pid）")
    sys.exit(0)
