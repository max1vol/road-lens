import measured from '@/data/evaluation-summary.json';
type Arm = {name:string;passed:number;total:number;completed:number;median_seconds:number;failures:string[]};
export default function EvaluationResults(){
 const arms = measured.arms as Arm[];
 return <details className="evaluation-results"><summary>Evaluation & reliability <span>Measured checks and limitations</span></summary>
  <div className="evaluation-body"><p>Fixed, authored scenarios · {measured.model} · same data, tools and budgets across three implementations.</p>
   {arms.length>0?<table><caption>One attempt per scenario. Failed runs remain in the denominator.</caption><thead><tr><th>Implementation</th><th>Passed</th><th>Completed</th><th>Median time</th></tr></thead><tbody>{arms.map(a=><tr key={a.name}><th>{a.name}</th><td>{a.passed}/{a.total}</td><td>{a.completed}/{a.total}</td><td>{a.median_seconds.toFixed(1)}s</td></tr>)}</tbody></table>:<p>The scored agent comparison has not yet completed. No accuracy improvement is claimed.</p>}
   <p className="muted">{measured.failure_note}</p>
   {arms.filter(a=>a.failures.length).map(a=><p key={a.name}><b>{a.name} failures:</b> {a.failures.join(', ')}.</p>)}
   <p><b>{measured.workflow_passed}/{measured.workflow_total} workflow scenarios passed</b> · cancellation, saved-response retries, conflict handling, role separation and recovery. <b>{measured.concurrency_checks_passed}/{measured.concurrency_checks_total} concurrency checks passed.</b></p>
   <p className="muted">Workflow checks used the real API with an isolated local database and simulated failure callbacks. They do not measure AI accuracy or prove a physical box rehearsal. The authored gold labels have not had independent human review.</p>
   <div className="evaluation-links"><a href={measured.artifacts_url} target="_blank" rel="noreferrer">Inspect results and failures ↗</a><a href={measured.fixture_hashes_url} target="_blank" rel="noreferrer">Frozen cases and data hashes ↗</a></div>
  </div>
 </details>;
}
