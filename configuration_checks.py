"""Deterministic ordinary-ARW combinations from the pinned check_a_mundo.

Keep input-dependent conditions in guidance rather than inventing availability.
No check mutates the resolved namelist or repairs a user's scientific choice.
"""

def combination_errors(values, count):
    data = values["input"]
    physics, dynamics = data.get("physics",{}), data.get("dynamics",{})
    domain, boundary = data.get("domains",{}), data.get("bdy_control",{})
    errors = []
    def get(group,key,index=0,default=0):
        value = group.get(key,default)
        return value[index] if isinstance(value,list) else value
    def reject(condition,message):
        if condition:
            errors.append(message+"；请明确调整关联参数，输入不会自动修改")
    for group, keys, zero_allowed in ((physics,("mp_physics","sf_surface_physics","sf_sfclay_physics","sf_urban_physics","ra_lw_physics","ra_sw_physics"),False),
                                     (physics,("bl_pbl_physics","cu_physics","gwd_opt"),True)):
        for key in keys:
            reject(any(get(group,key,i)!=get(group,key) and not(zero_allowed and get(group,key,i)==0) for i in range(1,count)),
                   f"{key} 跨域组合不受当前 WRF 支持：各域须与 D01 相同"+("，子域可关闭为 0" if zero_allowed else ""))
    for i in range(count):
        p=lambda key,default=0:get(physics,key,i,default)
        d=lambda key,default=0:get(dynamics,key,i,default)
        prefix=f"D{i+1:02d} "
        soil = {0:5,1:5,2:4,3:6,4:4,5:10,7:2}.get(p("sf_surface_physics"))
        reject(soil is not None and soil!=p("num_soil_layers",4),prefix+f"陆面 sf_surface_physics={p('sf_surface_physics')} 需要 {soil} 层土壤，当前受管资料／配置为 {p('num_soil_layers',4)} 层；当前可选 Noah(2) 或 Noah-MP(4)")
        reject(p("qna_update")!=0 and get(data.get("time_control",{}),"io_form_auxinput17")==0,prefix+"qna_update 需要 auxinput17 气溶胶更新通道，当前链路未接入；请关闭 qna_update")
        aero = 1 if p("use_aero_icbc") else 2 if p("use_rap_aero_icbc") else p("aer_init_opt")
        reject(p("mp_physics") in (28,29) and aero>1 and p("qna_update")==0,prefix+"气溶胶初始化 aer_init_opt>1 要求 qna_update=1 和 auxinput17；当前链路未接入更新通道，请选择无需该通道的初始化方式／微物理方案")
        reject(p("mp_physics") in (28,29) and p("wif_fire_emit") and aero in (0,1),prefix+"wif_fire_emit=true 需要含火排放的气溶胶来源 use_rap_aero_icbc=true，当前来源不含火排放")
        reject(d("km_opt")==5 and p("cu_physics")==11,prefix+"SMS-3DTKE（km_opt=5）与尺度感知 KF（cu_physics=11）不兼容")
        reject(p("aercu_opt")>0 and (p("cu_physics")!=11 or p("mp_physics")!=40),prefix+"aercu_opt>0 要求 cu_physics=11 且 mp_physics=40；当前常用微物理候选不含 40，请关闭 aercu_opt")
        reject(p("seaice_albedo_opt")==1 and p("sf_surface_physics") not in (2,4),prefix+"seaice_albedo_opt=1 要求 Noah(2) 或 Noah-MP(4) 陆面")
        reject(d("km_opt")==5 and d("diff_opt")!=2,prefix+"SMS-3DTKE（km_opt=5）要求 diff_opt=2")
        reject(d("km_opt")==5 and p("bl_pbl_physics")!=0,prefix+"SMS-3DTKE（km_opt=5）要求关闭边界层方案 bl_pbl_physics=0")
        reject(d("km_opt")==5 and p("sf_sfclay_physics") not in (0,1,5,91),prefix+"km_opt=5 仅支持 sf_sfclay_physics=0/1/5/91")
        reject(p("sf_urban_physics") in (2,3) and p("sf_surface_mosaic")==1,prefix+"城市冠层 2/3 与 sf_surface_mosaic=1 不兼容")
        reject(p("slucm_distributed_drag") and (p("sf_urban_physics")>1 or p("use_wudapt_lcz")==1),prefix+"slucm_distributed_drag 不能与城市冠层 2/3 或 use_wudapt_lcz=1 同用")
        reject(p("sf_surf_irr_scheme")==1 and p("sf_surface_physics")!=2,prefix+"渠道灌溉 sf_surf_irr_scheme=1 要求 Noah 陆面 sf_surface_physics=2")
        reject(p("sf_surf_irr_scheme") in (1,2,3) and p("irr_num_hours")<=0,prefix+"启用灌溉时 irr_num_hours 必须大于 0")
        reject(p("shcu_physics")==5 and p("bl_pbl_physics") not in (2,5),prefix+"Deng 浅对流 shcu_physics=5 要求 MYJ(2) 或 MYNN(5) 边界层")
        reject(p("shcu_physics")==5 and (p("icloud")==3 or p("icloud_bl")==1),prefix+"shcu_physics=5 不能与 icloud=3 或 icloud_bl=1 同用")
        reject(p("bl_pbl_physics")==5 and p("bl_mynn_edmf")>0 and (p("shcu_physics")>0 or p("ishallow")>0),prefix+"MYNN EDMF 要求 shcu_physics=0 且 ishallow=0")
        reject(p("icloud")==3 and p("mp_physics") in (1,3),prefix+"icloud=3 需要含 QICE 的微物理方案，不能选择 Kessler(1) 或 WSM3(3)")
        reject(p("fractional_seaice")==1 and p("sf_surface_physics")==1,prefix+"fractional_seaice=1 与简单热扩散陆面 sf_surface_physics=1 不兼容")
        reject(p("cu_diag")==1 and p("cu_physics") not in (3,5,10,93),prefix+"cu_diag=1 要求积云方案 cu_physics=3/5/10/93")
        reject(p("kf_edrates")==1 and p("cu_physics") not in (1,11,99),prefix+"kf_edrates=1 要求 KF 系列积云方案 cu_physics=1/11/99")
        reject((p("bl_pbl_physics")==10)!=(p("sf_sfclay_physics")==10),prefix+"TEMF 边界层与近地层必须同时选择编号 10")
        reject(p("tmn_update")==1 and p("lagday")!=150,prefix+"tmn_update=1 要求 lagday=150")
        reject(p("couple_farms") and p("swint_opt")!=2,prefix+"couple_farms=true 要求 swint_opt=2")
        reject(p("ra_sw_eclipse")==1 and p("ra_sw_physics") not in (1,2,4,5),prefix+"ra_sw_eclipse=1 仅支持短波方案 1/2/4/5")
    reject(dynamics.get("base_lapse",50)==0,"基态递减率 base_lapse=0 会使地面气压计算除零")
    for key,default in (("spec_zone",1),("relax_zone",4)):
        expected=1 if key=="spec_zone" else boundary.get("spec_bdy_width",5)-1
        reject(boundary.get(key,default)!=expected,f"真实资料边界要求 {key}={expected}（spec_bdy_width={boundary.get('spec_bdy_width',5)}）")
    reject(boundary.get("spec_bdy_width",5)<1,"spec_bdy_width 必须至少为 1")
    for i in range(count):
        reject(get(domain,"vert_refine_method",i)!=0 or domain.get("vert_refine_fact",1)!=1,
               f"D{i+1:02d} 当前工作台采用全域相同垂直网格，不支持 vert_refine_method／vert_refine_fact 垂直嵌套")
    diags=data.get("diags",{})
    for key in ("p_lev_diags","z_lev_diags"):
        reject(diags.get(key,0)!=0,f"{key} 需要专用 auxhist 输出通道，当前受管输出链尚未接入")
    return errors
