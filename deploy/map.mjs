// EPSG:3857 / slippy-map projection. All input geometry remains WGS84.
const MAP_ZOOM = 14;
export const SELECTED_COLOR = '#005a9c';
export const NEIGHBOR_COLOR = '#626b73';
export function worldPixel([longitude, latitude], zoom) {
  const size=256*2**zoom, phi=Math.max(-85.05112878,Math.min(85.05112878,latitude))*Math.PI/180;
  return [(longitude+180)/360*size,(1-Math.log(Math.tan(phi)+1/Math.cos(phi))/Math.PI)/2*size];
}
export function mapView(campus, visibility, zoom = 1, focus = null) {
  let points;
  if (visibility?.calculation_domain) points=visibility.calculation_domain.coordinates.flat();
  else points=campus.features.flatMap(f=>f.geometry.type==='MultiPolygon'?f.geometry.coordinates.flat(2):f.geometry.coordinates.flat());
  const xy=points.map(p=>worldPixel(p,MAP_ZOOM));
  let left=Math.min(...xy.map(p=>p[0])), right=Math.max(...xy.map(p=>p[0]));
  let top=Math.min(...xy.map(p=>p[1])), bottom=Math.max(...xy.map(p=>p[1]));
  // Legacy snapshots have no exterior domain; keep a usable surrounding view.
  if(!visibility?.calculation_domain){left-=220;right+=220;top-=220;bottom+=220;}
  const scale=Math.max(960/(right-left),560/(bottom-top))*Math.max(1,Math.min(16,zoom));
  const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
  const cx=clamp(focus?.[0]??(left+right)/2,left+480/scale,right-480/scale);
  const cy=clamp(focus?.[1]??(top+bottom)/2,top+280/scale,bottom-280/scale);
  return {scale,cx,cy,project:p=>{const [x,y]=worldPixel(p,MAP_ZOOM);return [480+(x-cx)*scale,280+(y-cy)*scale];}};
}
// Chronological neighbors, independent of table sorting and pagination. Never backfill.
export function overlayEvents(events, selected) {
  const ordered=[...events].sort((a,b)=>Date.parse(a.minimum_time)-Date.parse(b.minimum_time));
  const index=ordered.indexOf(selected);
  return index<0 ? [] : ordered.slice(Math.max(0,index-2),index+3).filter(e=>e.visibility);
}
export function nearestEvent(events, now=Date.now()) {
  return events.reduce((best,e)=>!best||Math.abs(Date.parse(e.minimum_time)-now)<Math.abs(Date.parse(best.minimum_time)-now)?e:best,null);
}
export function asLines(geometry) {
  if(!geometry||!geometry.coordinates?.length)return [];
  return geometry.type==='LineString'?[geometry.coordinates]:geometry.coordinates;
}

// Choose the next native level so raster tiles are never enlarged in CSS pixels.
// Keep the same z14 world coordinates for scientific geometry at every tile level.
function tileZoom(view, displayWidth) {
  return Math.max(14,Math.min(19,14+Math.ceil(Math.log2(Math.max(.01,view.scale*displayWidth/960)))));
}
export function exteriorTiles(view,displayWidth=960) {
  const zoom=tileZoom(view,displayWidth),tileSpan=256/2**(zoom-14);
  const minX=Math.floor((view.cx-480/view.scale)/tileSpan),maxX=Math.ceil((view.cx+480/view.scale)/tileSpan)-1;
  const minY=Math.floor((view.cy-280/view.scale)/tileSpan),maxY=Math.ceil((view.cy+280/view.scale)/tileSpan)-1;
  const tiles=[];
  for(let x=minX;x<=maxX;x++)for(let y=minY;y<=maxY;y++)tiles.push({
    url:`https://tile.openstreetmap.org/${zoom}/${x}/${y}.png`,zoom,
    x:480+(x*tileSpan-view.cx)*view.scale,y:280+(y*tileSpan-view.cy)*view.scale,size:tileSpan*view.scale+.2,
  });
  return tiles;
}
// Layer choices, language, projection, bounds and WMS parameters match
// https://map.sjtu.edu.cn/resources/jmap/sjtu_config.js and jmap.js:addWms.
// The source frontend composes WMS tiles in the browser; its labels are raster content.
export const CAMPUS_LAYERS=[
  {name:'jmap:minhang_2d',bounds:[121.4179,31.0183,121.4467,31.037]},
  {name:'jmap:nyby_2d',bounds:[121.4311,31.0358,121.4332,31.0384]},
];
export function campusTiles(view,displayWidth=960,pixelRatio=1){
  const zoom=tileZoom(view,displayWidth);
  // Request more pixels for the same geographic tile on dense displays, capped at 2×.
  const resolution=String(256*(pixelRatio>1?2:1));
  const factor=2**(zoom-14),world=256*2**zoom,tileSpan=256/factor,extent=20037508.342789244;
  const result=[];
  for(const layer of CAMPUS_LAYERS){
    const a=worldPixel([layer.bounds[0],layer.bounds[3]],zoom),b=worldPixel([layer.bounds[2],layer.bounds[1]],zoom);
    const minX=Math.max(Math.floor(a[0]/256),Math.floor((view.cx-480/view.scale)/tileSpan));
    const maxX=Math.min(Math.ceil(b[0]/256)-1,Math.ceil((view.cx+480/view.scale)/tileSpan)-1);
    const minY=Math.max(Math.floor(a[1]/256),Math.floor((view.cy-280/view.scale)/tileSpan));
    const maxY=Math.min(Math.ceil(b[1]/256)-1,Math.ceil((view.cy+280/view.scale)/tileSpan)-1);
    for(let x=minX;x<=maxX;x++)for(let y=minY;y<=maxY;y++){
      const left=x*256/world*2*extent-extent,right=(x+1)*256/world*2*extent-extent;
      const top=extent-y*256/world*2*extent,bottom=extent-(y+1)*256/world*2*extent;
      const url=new URL('https://map.sjtu.edu.cn/geoserver/jmap/wms');
      url.search=new URLSearchParams({service:'WMS',request:'GetMap',version:'1.1.1',layers:layer.name,styles:'',srs:'EPSG:3857',bbox:[left,bottom,right,top].join(','),width:resolution,height:resolution,format:'image/png',transparent:'true',tiled:'true'});
      result.push({url:url.href,zoom,x:480+(x*tileSpan-view.cx)*view.scale,y:280+(y*tileSpan-view.cy)*view.scale,size:tileSpan*view.scale+.2,layer:layer.name});
    }
  }
  return result;
}
