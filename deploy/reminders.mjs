import {eventKey, visibilityAt, present} from './core.mjs';

export const UPDATE_INTERVAL=5*60000;
export const REMINDER_INTERVAL=15000;
export const SETTINGS_KEY='transit-settings-v1';
export const SENT_KEY='transit-reminders-v1';
export const defaults={autoUpdate:true,nearbyNotifications:false,followDevice:false};

export function readSettings(storage) {
  try {
    const saved=JSON.parse(storage.getItem(SETTINGS_KEY));
    return Object.fromEntries(Object.entries(defaults).map(([key,value])=>[key,typeof saved?.[key]==='boolean'?saved[key]:value]));
  } catch {return {...defaults};}
}
export function nearbyEvents(events, location, manifest, now=Date.now()) {
  if(!location||!manifest||now<Date.parse(manifest.from_time)||now>=Date.parse(manifest.to_time))return [];
  return events.flatMap(event=>{
    const local=visibilityAt(event,location);
    if(!local||local.outside_chart||!present(local.distance_m)||local.distance_m>100)return [];
    // Outside the region there is no local transit; report the exported event time.
    const instant=local.minimum_time||event.minimum_time,t=Date.parse(instant);
    return t>=now&&t-now<=10*60000?[{event,instant,distance:local.distance_m,approximate:!local.minimum_time}]:[];
  }).sort((a,b)=>Date.parse(a.instant)-Date.parse(b.instant));
}
// A small orbit update should not notify twice for the same pass.
export function reminderKey(event) {return `${event.identifier}|${event.target}`;}
export function alreadyReminded(sent, event, instant) {
  const last=sent[reminderKey(event)];
  return Number.isFinite(last)&&Math.abs(last-Date.parse(instant))<20*60000;
}
export function notificationURL(event, href) {
  const url=new URL(href);url.hash=new URLSearchParams({event:eventKey(event)});return url.href;
}
