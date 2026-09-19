import { env, waitUntil } from 'cloudflare:workers';
import { timingSafeEqual, createHash } from 'node:crypto';
import { getChatGPTUser } from '@/app/chatgpt-auth';
type Settings={DB:D1Database;ROADLENS_DEVICE_TOKEN_SHA256?:string;ROADLENS_DEVICE_ID?:string;ROADLENS_INTERNAL_TOKEN?:string;ROADLENS_MODAL_SERVICE_TOKEN?:string;ROADLENS_MODAL_URL?:string;ROADLENS_OWNER_USER_ID?:string;ROADLENS_SITE_ORIGIN?:string};
export const settings=()=>env as unknown as Settings;
export const database=()=>{const db=settings().DB;if(!db)throw new ApiError(503,'Storage unavailable');return db;};
export class ApiError extends Error{constructor(public status:number,message:string){super(message);}}
export function json(value:unknown,status=200){return Response.json(value,{status,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});}
export const sha256=(s:string)=>createHash('sha256').update(s).digest('hex');
function equal(a:string,b:string){const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&timingSafeEqual(x,y);}
export function bearer(req:Request,role:'device'|'internal'){
 const token=req.headers.get('authorization')?.match(/^Bearer ([^\s]+)$/)?.[1]??'';
 const configured=role==='device'?settings().ROADLENS_DEVICE_TOKEN_SHA256:settings().ROADLENS_INTERNAL_TOKEN;
 const wanted=role==='device'?configured:sha256(configured??'');
 if(!token||!configured||!wanted||!equal(sha256(token),wanted))throw new ApiError(401,'Authentication required');
 return role==='device'?(settings().ROADLENS_DEVICE_ID??'aiy-box-1'):'internal';
}
export async function owner(req:Request,mutation=false){
 const user=await getChatGPTUser();if(!user)throw new ApiError(401,'Sign in with ChatGPT');
 if(!settings().ROADLENS_OWNER_USER_ID||!equal(user.userId,settings().ROADLENS_OWNER_USER_ID!))throw new ApiError(403,'Owner access required');
 if(mutation&&req.headers.get('origin')!==new URL(req.url).origin)throw new ApiError(403,'Same-origin request required');return user;
}
export async function body(req:Request,max=16384):Promise<unknown>{
 if(!req.headers.get('content-type')?.startsWith('application/json'))throw new ApiError(415,'JSON required');
 if(Number(req.headers.get('content-length')??0)>max)throw new ApiError(413,'Request too large');if(!req.body)throw new ApiError(400,'Body required');
 const reader=req.body.getReader(),chunks:Uint8Array[]=[];let length=0;
 while(true){const {value,done}=await reader.read();if(done)break;length+=value.byteLength;if(length>max){await reader.cancel();throw new ApiError(413,'Request too large');}chunks.push(value);}
 const buffer=new Uint8Array(length);let at=0;for(const c of chunks){buffer.set(c,at);at+=c.byteLength;}
 try{return JSON.parse(new TextDecoder().decode(buffer));}catch{throw new ApiError(400,'Invalid JSON');}
}
export async function rate(id:string,kind:string,limit:number){const now=Date.now(),bucket=`${id}:${kind}:${Math.floor(now/60000)}`;const row=await database().prepare('INSERT INTO rates(bucket,count,expires_at) VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1 RETURNING count').bind(bucket,now+120000).first<{count:number}>();if(!row||row.count>limit)throw new ApiError(429,'Please wait before trying again');}
export async function modal(path:string,payload:unknown,timeout=95000){const s=settings();if(!s.ROADLENS_MODAL_URL||!s.ROADLENS_MODAL_SERVICE_TOKEN)throw new ApiError(503,'Analysis unavailable');const r=await fetch(s.ROADLENS_MODAL_URL+path,{method:'POST',headers:{Authorization:'Bearer '+s.ROADLENS_MODAL_SERVICE_TOKEN,'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(timeout)});if(!r.ok)throw new ApiError(503,'Analysis unavailable');return r.json();}
export function wake(){waitUntil(modal('/wake',{},5000).catch(()=>{}));}
export function canonical(value:unknown):string{if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';if(value&&typeof value==='object')return '{'+Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>JSON.stringify(k)+':'+canonical(v)).join(',')+'}';return JSON.stringify(value);}
export function affirmative(text:string){const t=text.toLowerCase().trim().replace(/[’']/g, "'").replace(/[.!]+$/,'').trim();return /^(yes|yeah|yep|sure|okay|ok)(,? please)?(,? (submit it|submit that|submit the report|submit this|go ahead))?$|^(please )?(submit it|submit that|submit the report|go ahead)$/.test(t);}
