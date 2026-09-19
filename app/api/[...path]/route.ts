import { z } from 'zod';
import { getChatGPTUser } from '@/app/chatgpt-auth';
import { PrepareRequestSchema,SubmissionSchema,IntakeResponseSchema,OfficerQuestionSchema,JobResultSchema } from '@/contracts/generated';
import official from '@/data/public-evidence.json';
import { ApiError,json,database,settings,bearer,owner,body,rate,modal,wake,canonical,sha256,affirmative } from '@/lib/server';
export const dynamic='force-dynamic';
type Row=Record<string,any>;
const uid=()=>crypto.randomUUID();
const iso=(n:number)=>new Date(n).toISOString();
function publicReport(r:Row){const p=official.places.find(p=>p.place_id===r.place_id);return {report_id:r.id,category:r.issue,place_id:r.place_id,junction:p?.display_name??'Junction unavailable',latitude:p?.latitude,longitude:p?.longitude,received_at:iso(r.received_at),source:'Resident voice report',analysis_status:r.analysis_status,review_status:r.review_status};}
function receipt(r:Row){return {report_id:r.report_id,received_at:iso(r.received_at),analysis_status:'queued',review_status:'awaiting_officer_review'};}
async function handle(req:Request){
 const path=new URL(req.url).pathname.replace(/^\/api\//,'');const method=req.method;
 if(path==='health'&&method==='GET'){await database().prepare('SELECT 1').first();return json({status:'ok'});}
 if(path==='identity'&&method==='GET'){const u=await getChatGPTUser();return json({signed_in:!!u,user_id:u?.userId??null,owner:!!u&&!!settings().ROADLENS_OWNER_USER_ID&&u.userId===settings().ROADLENS_OWNER_USER_ID});}
 if(path==='public/evidence'&&method==='GET')return json(official);
 if(path==='public/reports'&&method==='GET'){
  const after=Number(new URL(req.url).searchParams.get('after')??0);if(!Number.isSafeInteger(after)||after<0)throw new ApiError(400,'Invalid cursor');
  const db=database();const page=await db.prepare('SELECT cursor,report_id FROM events WHERE cursor>? ORDER BY cursor LIMIT 100').bind(after).all<{cursor:number;report_id:string}>();
  const ids=[...new Set(page.results.map(e=>e.report_id))];
  const rows=ids.length?await db.prepare('SELECT * FROM reports WHERE id IN('+ids.map(()=>'?').join(',')+') ORDER BY received_at DESC').bind(...ids).all<Row>():{results:[]};
  return json({reports:rows.results.map(publicReport),cursor:page.results.at(-1)?.cursor??after,has_more:page.results.length===100});
 }
 if(path.startsWith('device/')){
  const device=bearer(req,'device');const db=database();
  if(path==='device/prepare'&&method==='POST'){
   await rate(device,'prepare',12);const input=PrepareRequestSchema.parse(await body(req));
   if(input.turns.map(t=>t.text).join('\n').length>4000||new Set(input.turns.map(t=>t.turn_id)).size!==input.turns.length)throw new ApiError(400,'Invalid resident turns');
   for(let i=0;i<input.turns.length;i++){if(Date.parse(input.turns[i].timestamp)>Date.now()+300000||(i&&Date.parse(input.turns[i].timestamp)<Date.parse(input.turns[i-1].timestamp)))throw new ApiError(400,'Invalid turn time');}
   const id=uid(),started=Date.now();
   await db.batch([
    db.prepare("UPDATE drafts SET status='cancelled' WHERE device_id=? AND session_id=? AND status IN('ready','preparing')").bind(device,input.session_id),
    db.prepare("INSERT INTO drafts(id,device_id,session_id,output,turns,run,created_at,expires_at,status) VALUES(?,?,?,'{}',?,'{}',?,?,'preparing')").bind(id,device,input.session_id,JSON.stringify(input.turns),started,started+900000),
   ]);
   let result;
   try{result=IntakeResponseSchema.parse(await modal('/prepare',input));}
   catch(e){await db.prepare("UPDATE drafts SET status='cancelled' WHERE id=? AND status='preparing'").bind(id).run();throw e;}
   if(result.output.kind!=='ready_for_confirmation'){await db.prepare("UPDATE drafts SET status='cancelled' WHERE id=? AND status='preparing'").bind(id).run();return json(result);}
   const now=Date.now(),expiry=now+900000;
   const stored=await db.prepare("UPDATE drafts SET output=?,run=?,created_at=?,expires_at=?,status='ready' WHERE id=? AND status='preparing' RETURNING id").bind(JSON.stringify(result.output),JSON.stringify(result.run),now,expiry,id).first();
   if(!stored)throw new ApiError(409,'Preparation superseded by a correction');
   return json({...result,draft_id:id,prepared_at:iso(now),expires_at:iso(expiry),readback:result.output.readback});
  }
  const cancel=path.match(/^device\/drafts\/([\w-]+)\/cancel$/);
  if(cancel&&method==='POST'){await body(req);await db.prepare("UPDATE drafts SET status='cancelled' WHERE id=? AND device_id=? AND status IN('ready','preparing')").bind(cancel[1],device).run();return json({cancelled:true});}
  if(path==='device/reports'&&method==='POST'){
   await rate(device,'submit',30);const input=SubmissionSchema.parse(await body(req)),key=req.headers.get('idempotency-key')??'';
   if(!/^[a-zA-Z0-9_.:-]{16,128}$/.test(key))throw new ApiError(400,'A valid Idempotency-Key is required');
   const hash=sha256(canonical(input));
   const previous=await db.prepare('SELECT * FROM receipts WHERE device_id=? AND key=?').bind(device,key).first<Row>();
   if(previous){if(previous.request_hash!==hash)throw new ApiError(409,'Idempotency key already used for different content');return json(receipt(previous));}
   const d=await db.prepare('SELECT * FROM drafts WHERE id=? AND device_id=?').bind(input.draft_id,device).first<Row>();
   if(!d)throw new ApiError(404,'Draft not found');
   const now=Date.now(),confirmed=Date.parse(input.confirmation.confirmed_at);
   if(d.status!=='ready'||d.consumed_report_id)throw new ApiError(409,'Draft has been cancelled or consumed');
   if(d.expires_at<now)throw new ApiError(409,'Draft expired; prepare and confirm again');
   if(!affirmative(input.confirmation.text)||confirmed<=d.created_at||confirmed>now+300000||JSON.parse(d.turns).some((t:Row)=>t.turn_id===input.confirmation.resident_turn_id))throw new ApiError(409,'A fresh affirmative confirmation is required');
   const report=JSON.parse(d.output),id=uid(),job=uid();
   try{
    await db.batch([
     db.prepare("INSERT INTO reports(id,draft_id,issue,place_id,received_at,updated_at) SELECT ?,id,?,?,?,? FROM drafts WHERE id=? AND device_id=? AND status='ready' AND consumed_report_id IS NULL AND expires_at>=?").bind(id,report.issue,report.location.place_id,now,now,input.draft_id,device,now),
     db.prepare("UPDATE drafts SET status='consumed',consumed_report_id=? WHERE id=? AND EXISTS(SELECT 1 FROM reports WHERE id=?)").bind(id,input.draft_id,id),
     db.prepare('INSERT INTO receipts(device_id,key,request_hash,report_id,received_at) SELECT ?,?,?,id,received_at FROM reports WHERE id=?').bind(device,key,hash,id),
     db.prepare("INSERT INTO jobs(id,kind,report_id,payload,created_at,updated_at) SELECT ?,'report_analysis',id,?,?,? FROM reports WHERE id=?").bind(job,JSON.stringify({report}),now,now,id),
     db.prepare("INSERT INTO events(report_id,type,created_at) SELECT id,'received',? FROM reports WHERE id=?").bind(now,id),
    ]);
   }catch{
    // A concurrent retry may win the unique receipt constraint; inspect that exact key.
    const concurrent=await db.prepare('SELECT * FROM receipts WHERE device_id=? AND key=?').bind(device,key).first<Row>();
    if(concurrent&&concurrent.request_hash===hash)return json(receipt(concurrent));
    throw new ApiError(409,'Submission conflicted; retry the same confirmed request');
   }
   const saved=await db.prepare('SELECT * FROM receipts WHERE device_id=? AND key=?').bind(device,key).first<Row>();
   if(!saved)throw new ApiError(409,'Draft already consumed');
   if(saved.request_hash!==hash)throw new ApiError(409,'Idempotency key already used for different content');
   await wake();return json(receipt(saved),saved.report_id===id?201:200);
  }
  throw new ApiError(404,'Route not found');
 }
 if(path.startsWith('internal/')){
  bearer(req,'internal');const db=database(),now=Date.now();
  if(path==='internal/jobs/claim'&&method==='POST'){
   await body(req);
   // Expired third attempts terminate visibly. Claim/update/result each use atomic statements/batches.
   await db.batch([
    db.prepare("UPDATE jobs SET state='failed',error='analysis_unavailable',updated_at=? WHERE state='running' AND lease_expires<? AND attempts>=3").bind(now,now),
    db.prepare("INSERT INTO events(report_id,type,created_at) SELECT r.id,'analysis_failed',? FROM reports r JOIN jobs j ON j.report_id=r.id WHERE j.state='failed' AND r.analysis_status!='failed'").bind(now),
    db.prepare("UPDATE reports SET analysis_status='failed',updated_at=? WHERE id IN(SELECT report_id FROM jobs WHERE state='failed') AND analysis_status!='failed'").bind(now),
    db.prepare('DELETE FROM rates WHERE expires_at<?').bind(now),
   ]);
   const token=uid();
   const j=await db.prepare("UPDATE jobs SET state='running',attempts=attempts+1,lease_token=?,lease_expires=?,updated_at=? WHERE id=(SELECT id FROM jobs WHERE (state='queued' OR (state='running' AND lease_expires<?)) AND attempts<3 ORDER BY created_at LIMIT 1) RETURNING *").bind(token,now+120000,now,now).first<Row>();
   if(!j)return json({job:null});
   if(j.report_id)await db.batch([db.prepare("UPDATE reports SET analysis_status='running',updated_at=? WHERE id=? AND EXISTS(SELECT 1 FROM jobs WHERE id=? AND state='running' AND lease_token=?)").bind(now,j.report_id,j.id,token),db.prepare("INSERT INTO events(report_id,type,created_at) SELECT ?,'analysis_running',? WHERE changes()=1").bind(j.report_id,now)]);
   return json({job:{id:j.id,kind:j.kind,payload:JSON.parse(j.payload),lease_token:token,attempts:j.attempts,lease_expires:iso(now+120000)}});
  }
  const action=path.match(/^internal\/jobs\/([\w-]+)\/(renew|result)$/);
  if(action&&method==='POST'){
   const [,id,op]=action;
   if(op==='renew'){
    const b=z.object({lease_token:z.string().max(128)}).strict().parse(await body(req));
    const j=await db.prepare("UPDATE jobs SET lease_expires=? WHERE id=? AND state='running' AND lease_token=? AND lease_expires>=? RETURNING id").bind(now+120000,id,b.lease_token,now).first();
    if(!j)throw new ApiError(409,'Lease no longer active');return json({renewed:true,lease_expires:iso(now+120000)});
   }
   const b=JobResultSchema.parse(await body(req,512*1024));if((b.result===null)===(b.error===null))throw new ApiError(400,'One outcome required');
   const j=await db.prepare('SELECT * FROM jobs WHERE id=?').bind(id).first<Row>();if(!j)throw new ApiError(404,'Job not found');
   if(j.state==='complete'&&j.lease_token===b.lease_token&&canonical(JSON.parse(j.result))===canonical(b.result))return json({accepted:true,duplicate:true});
   if(j.state!=='running'||j.lease_token!==b.lease_token||j.lease_expires<now)throw new ApiError(409,'Lease no longer active');
   const state=b.result?'complete':j.attempts>=3?'failed':'queued';const result=b.result?JSON.stringify(b.result):null;
   const written=await db.batch([
    db.prepare("UPDATE jobs SET state=?,result=?,error=?,updated_at=? WHERE id=? AND state='running' AND lease_token=? AND lease_expires>=?").bind(state,result,b.error,now,id,b.lease_token,Date.now()),
    db.prepare('UPDATE reports SET analysis_status=?,updated_at=? WHERE id=? AND changes()=1').bind(state,now,j.report_id),
    db.prepare('INSERT INTO events(report_id,type,created_at) SELECT report_id,?,? FROM jobs WHERE id=? AND report_id IS NOT NULL AND state=? AND lease_token=? AND updated_at=? AND changes()=1').bind('analysis_'+state,now,id,state,b.lease_token,now),
   ]);
   if(!written[0].meta.changes){const latest=await db.prepare('SELECT * FROM jobs WHERE id=?').bind(id).first<Row>();if(latest?.state==='complete'&&latest.lease_token===b.lease_token&&canonical(JSON.parse(latest.result))===canonical(b.result))return json({accepted:true,duplicate:true});throw new ApiError(409,'Lease no longer active');}
   return json({accepted:true,state});
  }
  throw new ApiError(404,'Route not found');
 }
 if(path.startsWith('officer/')){
  const user=await owner(req,method==='POST');const db=database(),now=Date.now();
  if(path==='officer/ask'&&method==='POST'){
   await rate(user.userId,'ask',15);const b=OfficerQuestionSchema.parse(await body(req));let report=null;
   if(b.report_id){const d=await db.prepare('SELECT d.output FROM drafts d JOIN reports r ON r.draft_id=d.id WHERE r.id=?').bind(b.report_id).first<Row>();if(!d)throw new ApiError(404,'Report not found');report=JSON.parse(d.output);}
   const id=uid();await db.prepare("INSERT INTO jobs(id,kind,owner_id,payload,created_at,updated_at) VALUES(?,'officer_question',?,?,?,?)").bind(id,user.userId,JSON.stringify({question:b.question,report}),now,now).run();await wake();return json({job_id:id,state:'queued'},202);
  }
  const job=path.match(/^officer\/jobs\/([\w-]+)$/);
  if(job&&method==='GET'){const j=await db.prepare('SELECT * FROM jobs WHERE id=? AND owner_id=?').bind(job[1],user.userId).first<Row>();if(!j)throw new ApiError(404,'Job not found');return json({job_id:j.id,state:j.state,result:j.result?JSON.parse(j.result):null,error:j.error});}
  const rep=path.match(/^officer\/reports\/([\w-]+)(?:\/(retry|review))?$/);
  if(rep){const [,id,op]=rep;
   if(!op&&method==='GET'){const r=await db.prepare('SELECT r.*,d.output,d.run,j.result,j.error,j.id AS job_id FROM reports r JOIN drafts d ON d.id=r.draft_id LEFT JOIN jobs j ON j.report_id=r.id WHERE r.id=?').bind(id).first<Row>();if(!r)throw new ApiError(404,'Report not found');return json({...publicReport(r),report:JSON.parse(r.output),intake_run:JSON.parse(r.run),analysis:r.result?JSON.parse(r.result):null,error:r.error});}
   if(op==='retry'&&method==='POST'){await body(req);await db.batch([db.prepare("UPDATE jobs SET state='queued',attempts=0,lease_token=NULL,lease_expires=NULL,error=NULL,updated_at=? WHERE report_id=? AND state='failed'").bind(now,id),db.prepare("UPDATE reports SET analysis_status='queued',updated_at=? WHERE id=? AND analysis_status='failed' AND EXISTS(SELECT 1 FROM jobs WHERE report_id=? AND state='queued')").bind(now,id,id),db.prepare("INSERT INTO events(report_id,type,created_at) SELECT id,'retry_requested',? FROM reports WHERE id=?").bind(now,id)]);await wake();return json({retry_requested:true});}
   if(op==='review'&&method==='POST'){const b=z.object({status:z.enum(['reviewed','archived'])}).strict().parse(await body(req));const r=await db.prepare('UPDATE reports SET review_status=?,updated_at=? WHERE id=? RETURNING id').bind(b.status,now,id).first();if(!r)throw new ApiError(404,'Report not found');await db.prepare('INSERT INTO events(report_id,type,created_at) VALUES(?,?,?)').bind(id,b.status,now).run();return json({review_status:b.status});}
  }
  throw new ApiError(404,'Route not found');
 }
 throw new ApiError(404,'Route not found');
}
async function safe(req:Request){try{return await handle(req);}catch(e){if(e instanceof ApiError)return json({error:e.message},e.status);if(e instanceof z.ZodError)return json({error:'Payload does not match the RoadLens contract'},400);console.error('RoadLens request failed',e instanceof Error?e.name:'UnknownError');return json({error:'Service unavailable; please retry'},503);}}
export const GET=safe;export const POST=safe;
