const root=document.getElementById("job-state");
let after=0; const id=root.dataset.jobId; const initialState=root.dataset.jobState;
async function refresh(){
  const r=await fetch("/api/jobs/"+id+"?after="+after);
  if(!r.ok)return;
  const d=await r.json();
  const j=d.job;
  if(j.state!==initialState && (j.state==="awaiting_input" || initialState==="awaiting_input" || ["succeeded","failed","interrupted"].includes(j.state))){
    location.reload();
    return;
  }
  document.querySelector("#job-message").textContent=j.message;
  document.querySelector("#job-phase").textContent=j.phase;
  const state=document.querySelector("#job-state");
  state.textContent=j.state;
  state.className="status large "+j.state;
  const bar=document.querySelector("#progress-bar");
  const terminal=["succeeded","failed","interrupted"].includes(j.state);
  if(j.total_items&&j.current_index){
    bar.classList.remove("indeterminate");
    bar.style.width=(j.current_index/j.total_items*100)+"%";
    document.querySelector("#job-count").textContent=j.current_index+" / "+j.total_items;
  }else if(terminal){
    bar.classList.remove("indeterminate");
    bar.style.width=j.state==="succeeded"?"100%":"0";
    document.querySelector("#job-count").textContent=j.state==="succeeded"?"Concluído":"Encerrado";
  }else if(j.state==="awaiting_input"){
    bar.classList.remove("indeterminate");
    document.querySelector("#job-count").textContent="Aguardando decisão";
  }else{
    bar.classList.add("indeterminate");
    bar.style.width="";
    document.querySelector("#job-count").textContent=j.message||"Em andamento";
  }
  const out=document.querySelector("#job-result");
  if(out&&(j.result||j.error_message)){
    document.querySelector("#job-outcome").hidden=false;
    const err=document.querySelector("#job-error");
    if(err) err.textContent=j.error_message||"";
    if(j.result) out.textContent=JSON.stringify(j.result,null,2);
    else out.textContent=j.error_message||"";
  }
  d.events.forEach(e=>{
    after=Math.max(after,e.event_id);
    const row=document.createElement("div");
    const phase=document.createElement("b");
    const message=document.createElement("span");
    phase.textContent=e.phase||"";
    message.textContent=e.message||"";
    row.append(phase,message);
    document.querySelector("#events").prepend(row);
  });
  const cont=document.querySelector("#job-continue");
  const clink=document.querySelector("#job-continue-link");
  if(cont&&clink&&j.state==="succeeded"&&d.continue_href){cont.hidden=false;clink.href=d.continue_href;}
  if(terminal)clearInterval(timer);
}
const timer=setInterval(refresh,1000);
refresh();
