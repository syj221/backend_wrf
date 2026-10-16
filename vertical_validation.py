"""Compute-free checks transcribed from the pinned ARW compute_eta/levels routines.

The new automatic algorithm uses default REAL (float32), including intermediate
operations. This checks initialization geometry, not terrain or forcing coverage.
"""
import math
import numpy as np

SOURCE = "WRF V4.7.1 dyn_em/module_initialize_real.F:compute_eta/levels"
F = np.float32


def automatic_levels(levels, top, maximum, bottom, surface, upper):
    """Replay levels(); return the original fatal class or computed eta."""
    n = levels - 1
    top, maximum, bottom, surface, upper = map(F, (top, maximum, bottom, surface, upper))
    rd, gravity, temperature, pressure = map(F, (287, 9.81, 290, 100000))
    height = rd * temperature / gravity * np.log(pressure / top)
    dz = bottom
    z = dz
    eta = [F(1), (pressure * np.exp(-gravity*z/rd/temperature)-top)/(pressure-top)]
    saved = 1
    for i in range(1, n):
        stretch = upper + (surface-upper)*max((maximum*F(.5)-dz)/(maximum*F(.5)), F(0))
        dz = stretch*dz
        required = (height-z)/F(n-saved)
        if required < dz:
            break
        saved = i+1
        z = z+dz
        eta.append((pressure*np.exp(-gravity*z/rd/temperature)-top)/(pressure-top))
        if i == n-1:
            return "unreachable", None, None
    dz = (height-z)/F(n-saved)
    if dz > F(1.5)*maximum:
        return "thick", float(dz), None
    for _ in range(saved, n):
        z = z+dz
        eta.append((pressure*np.exp(-gravity*z/rd/temperature)-top)/(pressure-top))
    eta[-1] = F(0)
    if any(not np.isfinite(v) for v in eta) or any(a <= b for a,b in zip(eta,eta[1:])):
        return "nonmonotonic", float(dz), None
    return None, float(dz), [float(v) for v in eta]


def old_levels(levels, top, maximum, dynamics):
    """Replay both thickness checks in compute_eta(auto_levels_opt=1)."""
    if levels < 12:
        return "old_size", None, None
    p00 = float(F(dynamics.get("base_pres", 100000)))
    t00 = float(F(dynamics.get("base_temp", 290)))
    lapse = float(F(dynamics.get("base_lapse", 50)))
    iso = float(F(dynamics.get("iso_temp", 200)))
    strat = float(F(dynamics.get("base_pres_strat", 0)))
    strat_lapse = float(F(dynamics.get("base_lapse_strat", -11)))
    rd, cp, gravity = map(F, (287, 1004.5, 9.81))
    cvpm = F(F(F(1004.5)-rd)/F(1004.5))
    cvpm = -cvpm
    top = float(F(top))
    mu = p00-top
    def volume(eta):
        pressure = eta*mu+top
        temp = max(iso, t00+lapse*math.log(pressure/p00))
        if pressure < strat:
            temp = iso+strat_lapse*math.log(pressure/strat)
        theta = temp*(p00/pressure)**float(rd/cp)-300
        return float(rd/F(100000))*(theta+300)*(pressure/100000)**float(cvpm)
    practice = [1,.993,.983,.970,.954,.934,.909,.88,.85,.8,.75,.7,.65,.6,.55,.5,.45,.4,.35,.3,.25,.2,.15,.1,.08,.06,.04,.02,.015,.01,.009,.008,.007,.006,.005,.004,.0035,.003,.0028,.0026,.0024,.0022,.002,.0018,.0016,.0014,.0012,.001,.0009,.0008,.0007,.0006,.0005,.0004,.0003,.0002,.0001,.00005,0]
    heights = [0.]
    for a,b in zip(practice,practice[1:]):
        heights.append(heights[-1]-(b-a)*mu*volume((a+b)*.5)/float(gravity))
    dz = (heights[-1]-heights[7])/(levels-8)
    if dz >= maximum:
        return "old_thick", dz, None
    eta = [0.]*(levels+1)  # Use the Fortran one-based indices below.
    znw = [F(0)]*(levels+1)
    alb = [0.]*(levels+1)
    for k in range(1,9):
        eta[k] = float(F(practice[k-1]))
    for k in range(8,levels-2):
        next_eta = next(v for v in practice if v < eta[k])
        alb[k] = volume(.5*(eta[k]+next_eta))
        eta[k+1] = float(F(eta[k]-dz*float(gravity)/(mu*alb[k])))
        alb[k] = volume(.5*(eta[k]+eta[k+1]))
        eta[k+1] = float(F(eta[k]-dz*float(gravity)/(mu*alb[k])))
    saved_alb = alb[levels-3]
    for k in range(1,levels-2):
        znw[k] = F(eta[k])
    for _ in range(5):
        for _ in range(10):
            for k in range(8,levels-3):
                alb[k] = volume(float(F(F(znw[k]+znw[k+1])*F(.5))))
                znw[k+1] = F(float(znw[k])-dz*float(gravity)/(mu*alb[k]))
            alb[levels-3] = saved_alb
            znw[levels-2] = F(0)
        for k in range(1,levels-2):
            alb[k] = volume(float(F(F(znw[k]+znw[k+1])*F(.5))))
        heights = [0.,0.]
        for k in range(2,levels-1):
            heights.append(heights[-1]-float(F(znw[k]-znw[k-1]))*mu*alb[k-1]/float(gravity))
        dz = (heights[levels-2]-heights[8])/(levels-10)
    return ("old_thick" if dz > maximum else None), dz, None


def validate_vertical(domain, dynamics):
    """Return actionable errors without changing any caller-owned values."""
    errors = []
    levels = domain["e_vert"][0]
    top = domain.get("p_top_requested", 5000)
    base = dynamics.get("base_pres", 100000)
    if not 0 < top < min(100000, base):
        return [f"模式顶气压 p_top_requested={top:g} Pa 必须大于 0 且小于基态地面气压 {min(100000,base):g} Pa；减小模式顶气压，实际资料的最高层覆盖仍待核验"]
    for key in ("base_pres", "base_temp", "iso_temp"):
        if dynamics.get(key, {"base_pres":100000,"base_temp":290,"iso_temp":200}[key]) <= 0:
            errors.append(f"{key} 必须大于 0；请恢复有效的基态气压／温度")
    if dynamics.get("base_pres_strat",0) < 0:
        errors.append("base_pres_strat 不能为负；0 表示不启用指定平流层基态")
    eta = domain.get("eta_levels")
    if eta is not None:
        if (not isinstance(eta,list) or len(eta)!=levels or eta[0]!=1 or eta[-1]!=0
                or any(a <= b for a,b in zip(eta,eta[1:]))):
            errors.append(f"eta_levels 须有 {levels} 项，从 1 严格递减到 0；请同步层数与显式分层表")
        elif any(F(a)<=F(b) for a,b in zip(eta,eta[1:])):
            errors.append("eta_levels 在 WRF 单精度下有重合层；请增大相邻 eta 间距")
        return errors
    mode = domain.get("auto_levels_opt",2)
    if mode not in (1,2):
        return errors+[f"auto_levels_opt={mode} 不受 WRF 支持；请选择 1（旧自动分层）或 2（自动拉伸分层）"]
    params = {k:domain.get(k,v) for k,v in {"max_dz":1000,"dzbot":50,"dzstretch_s":1.3,"dzstretch_u":1.1}.items()}
    for key,value in params.items():
        if value<=0 or not np.isfinite(F(value)) or F(value)==0:
            errors.append(f"{key}={value:g} 必须是 WRF 单精度可表示的正数；请调整该分层参数")
    if errors:
        return errors
    try:
        with np.errstate(all="raise"):
            if mode==2:
                failure,thickness,_ = automatic_levels(levels,top,*params.values())
            else:
                failure,thickness,_ = old_levels(levels,top,params["max_dz"],dynamics)
    except (ValueError,OverflowError,FloatingPointError,ZeroDivisionError,StopIteration):
        return ["垂直分层组合产生无效气压、温度或数值溢出；请检查模式顶、基态参数及层厚／拉伸因子"]
    description = f"e_vert={levels}，p_top_requested={top:g} Pa，max_dz={params['max_dz']:g} m，dzbot={params['dzbot']:g} m，dzstretch_s/u={params['dzstretch_s']:g}/{params['dzstretch_u']:g}"
    if failure == "unreachable":
        minimum = next((n for n in range(levels+1,501) if automatic_levels(n,top,*params.values())[0] is None),None)
        suggestion = f"保持其余分层参数时，层数至少设为 {minimum}" if minimum else "请联合调整层数和层厚／拉伸因子"
        errors.append(f"自动分层无法到达模型顶（{description}）：not enough eta levels to reach p_top。{suggestion}；或增大模型顶气压以降低模型高度，或调整 dzbot／拉伸因子。参数不会自动修改")
    elif failure in {"thick","old_thick"}:
        limit = params['max_dz']*(1.5 if mode==2 else 1)
        errors.append(f"自动分层顶部层厚 {thickness:.2f} m 超过该算法限制 {limit:g} m（{description}）；请增加垂直层数、增大模型顶气压以降低模型高度，或调整 max_dz／分层参数")
    elif failure == "old_size":
        errors.append("旧自动分层 auto_levels_opt=1 使用至第 12 层的固定索引，e_vert 须至少 12；请增加层数或选择 auto_levels_opt=2 后重新校验")
    elif failure:
        errors.append(f"自动分层生成重合或逆序 eta 层（{description}）；请调整模型顶和层厚／拉伸因子")
    return errors
