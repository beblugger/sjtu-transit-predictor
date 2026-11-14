// Reconstruct each clipped visibility band in the local metre chart.
function clip(vertices, nx, ny, limit) {
  const result = [];
  for (let i = 0; i < vertices.length; i++) {
    const a = vertices[i], b = vertices[(i + 1) % vertices.length];
    const da = nx*a[0] + ny*a[1] - limit, db = nx*b[0] + ny*b[1] - limit;
    if (da <= 0) result.push(a);
    if ((da <= 0) !== (db <= 0)) {
      const t = da / (da - db);
      result.push([a[0] + t*(b[0]-a[0]), a[1] + t*(b[1]-a[1])]);
    }
  }
  return result;
}

function centerSegment(polygon, nx, ny, offset) {
  const points = [];
  for (let i = 0; i < polygon.length; i++) {
    const a = polygon[i], b = polygon[(i+1)%polygon.length];
    const da = nx*a[0]+ny*a[1]-offset, db = nx*b[0]+ny*b[1]-offset;
    if (Math.abs(da) < 1e-7) points.push(a);
    if (da*db < 0) {const t = da/(da-db); points.push([a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1])]);}
  }
  return points.length >= 2 ? [points[0], points.at(-1)] : null;
}

export function inflateBands(event) {
  const v = event.visibility;
  if (!v?.bands || v.geometry) return;
  const vector = (value, length) => Array.isArray(value) && value.length === length && value.every(Number.isFinite);
  const bounds = value => vector(value, 4) && value[0] < value[2] && value[1] < value[3];
  if (!v.chart || ![v.chart.longitude, v.chart.latitude, v.chart.east_m_per_degree, v.chart.north_m_per_degree].every(Number.isFinite) ||
      v.chart.east_m_per_degree <= 0 || v.chart.north_m_per_degree <= 0 || !bounds(v.domain_m) ||
      !Array.isArray(v.bands) || !v.bands.length || v.bands.some(band => !band || !bounds(band.bounds_m) ||
        !vector(band.normal, 2) || Math.hypot(...band.normal) < 1e-12 || !Number.isFinite(band.offset_m) ||
        !Number.isFinite(band.half_width_m) || band.half_width_m < 0 || !vector(band.origin_m, 2) ||
        !Number.isFinite(band.time_offset_s) || !vector(band.time_gradient_s_per_m, 2) ||
        (band.clip !== undefined && (!vector(band.clip, 3) || Math.hypot(band.clip[0], band.clip[1]) < 1e-12)))) {
    throw new Error('事件可见区域数据格式无效');
  }
  const {longitude, latitude, east_m_per_degree: sx, north_m_per_degree: sy} = v.chart;
  const geo = p => [longitude + p[0]/sx, latitude + p[1]/sy];
  const [xmin,ymin,xmax,ymax] = v.domain_m;
  const rectangle = [[xmin,ymin],[xmax,ymin],[xmax,ymax],[xmin,ymax],[xmin,ymin]].map(geo);
  v.calculation_domain = {type:'Polygon',coordinates:[rectangle]};
  const polygons=[], contacts=[], centers=[];
  v.models ??= v.bands.map(b => ({bounds_m:b.bounds_m, origin_m:b.origin_m,
    time_offset_s:b.time_offset_s, time_gradient_s_per_m:b.time_gradient_s_per_m}));
  for (const band of v.bands) {
    const [a,b,c,d]=band.bounds_m, [nx,ny]=band.normal, offset=band.offset_m, half=band.half_width_m;
    const left=Math.max(a,xmin), right=Math.min(c,xmax), bottom=Math.max(b,ymin), top=Math.min(d,ymax);
    if (left >= right || bottom >= top) continue;
    let polygon=[[left,bottom],[right,bottom],[right,top],[left,top]];
    polygon=clip(polygon,nx,ny,offset+half);
    polygon=clip(polygon,-nx,-ny,-offset+half);
    if (band.clip) polygon=clip(polygon,...band.clip);
    if (polygon.length < 3) continue;
    const line=centerSegment(polygon,nx,ny,offset); if (line) centers.push(line.map(geo));
    for (let i=0; i<polygon.length; i++) {
      const p=polygon[i], q=polygon[(i+1)%polygon.length];
      const u=nx*p[0]+ny*p[1]-offset, w=nx*q[0]+ny*q[1]-offset;
      if ((Math.abs(u-half)<0.001 && Math.abs(w-half)<0.001) || (Math.abs(u+half)<0.001 && Math.abs(w+half)<0.001)) contacts.push([geo(p),geo(q)]);
    }
    polygons.push([[...polygon.map(geo),geo(polygon[0])]]);
  }
  v.geometry={type:'MultiPolygon',coordinates:polygons};
  v.boundaries={type:'MultiLineString',coordinates:contacts};
  v.centerline={type:'MultiLineString',coordinates:centers};
  v.reference_time ??= event.minimum_time;
}
