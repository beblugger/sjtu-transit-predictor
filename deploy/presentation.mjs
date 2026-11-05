// Weather labels describe the deterministic forecast, never the composite score.
export function weatherCondition(snapshot, instant) {
  const series=snapshot?.deterministic?.hourly,t=Date.parse(instant);
  const unknown={kind:'unavailable',label:'天气不可用',cloud:null,precipitation:null,code:null};
  if(!series?.times?.length||!Number.isFinite(t))return unknown;
  let lo=0,hi=series.times.length;
  while(lo<hi){const mid=(lo+hi)>>1;if(Date.parse(series.times[mid])<t)lo=mid+1;else hi=mid;}
  const candidates=[lo-1,lo].filter(i=>i>=0&&i<series.times.length);
  const i=candidates.reduce((a,b)=>Math.abs(Date.parse(series.times[a])-t)<=Math.abs(Date.parse(series.times[b])-t)?a:b);
  if(i===undefined||Math.abs(Date.parse(series.times[i])-t)>30*60000)return unknown;
  const cloud=series.cloud_cover_pct?.[i],code=series.weather_code?.[i],precipitation=series.precipitation_mm?.[i];
  const valid=v=>typeof v==='number'&&Number.isFinite(v);
  let kind,label;
  if(valid(code)&&[0,1].includes(code)){kind='clear';label='晴天';}
  else if(code===2){kind='overcast';label='阴天';}
  else if(code===3){kind='overcast';label='阴天';}
  else if(valid(code)&&code>=51){kind='overcast';label='阴天';}
  else if(valid(code)&&[45,48].includes(code)){kind='overcast';label='阴天';}
  else if(valid(cloud)){kind=cloud<=20?'clear':'overcast';label=kind==='clear'?'晴天':'阴天';}
  else return unknown;
  return {kind,label,cloud:valid(cloud)?cloud:null,precipitation:valid(precipitation)?precipitation:null,code:valid(code)?code:null,time:series.times[i]};
}
export function visibleDetailEntries(entries) {
  return entries.filter(([,value])=>value!==null&&value!==undefined&&String(value).trim()!==''&&!/未知|不可用|未设置|缺少|无法判断|尚未量化|^unknown$/i.test(String(value)));
}
export function dataNotices({manifest,selected,backend,loadError='',now=Date.now()}) {
  const notices=[];
  const add=(level,text)=>notices.push({level,text});
  if(backend?.state==='missing')add('critical','自动更新任务不存在，后续预报无法自动生成。');
  if(backend?.state?.startsWith('disabled'))add('critical','自动更新任务已停用，后续预报不会自动生成。');
  if(backend?.failure)add('critical',`最近一次自动更新失败（${backend.failure}），请查看构建状态。`);
  if(loadError)add('critical',loadError);
  if(manifest){
    if(now>=Date.parse(manifest.to_time))add('critical','预报覆盖已经结束，请检查自动更新任务。历史数据仍可查看。');
    else if(now<Date.parse(manifest.from_time))add('warning','设备时间早于预报起点，请检查设备时钟。');
    else add('info','当前时间位于预报覆盖范围内。');
    for(const text of manifest.warnings||[])add('warning',text);
    if(manifest.weather_status?.stale)add('warning','天气数据在本批生成时已过期。');
  }
  if(backend?.state==='unknown')add('warning','暂时无法核实自动更新任务状态；这不代表任务已停止。');
  for(const code of [...new Set([...(selected?.reason_codes||[]),...(selected?.weather?.reasons||[])])]){
    const level=['object_size_unknown_centerline_only'].includes(code)?'info':'warning';
    notices.push({level,code});
  }
  if(selected?.visibility)add('info','反解区域按导出的观测高度计算。采样回算偏差是局部展开的数值误差，不是轨道真实误差或全域严格误差上界。');
  return notices.sort((a,b)=>['critical','warning','info'].indexOf(a.level)-['critical','warning','info'].indexOf(b.level));
}
