import {el} from '../dom.js';
import {mixer} from '../audio-api.js';
import {baselineStates,sampleLabels,verdict} from '../audio-state.js';
import {field,run} from '../audio-form.js';
import {status,titleBlock} from '../widgets.js';
import {badge} from '../components/badge.js';

function score(entry,key) {
  return el('td',{},el('span',{class:'mono'},entry[key].toFixed(3)+' '),badge(verdict,String(entry[key+'_ok'])));
}
function comparisonView(item) {
  const rows=item.samples.map(entry=>el('tr',{class:entry.duty_ok===entry.candidate_ok?'':'diff'},
    el('td',{},el('code',{},entry.window)),el('td',{},entry.finding),
    el('td',{},badge(sampleLabels,entry.label)),score(entry,'duty'),score(entry,'candidate')));
  return el('details',{class:'clip'},el('summary',{},item.candidate+' 对照 '+item.against+' · '+
    (item.passed?'比对通过':'未通过')+' · '+item.samples.length+' 个已确认样本'),
    rows.length?el('div',{class:'scroll'},el('table',{},
      el('thead',{},el('tr',{},['样本窗','发现','判定','当班评分','候选评分'].map(x=>el('th',{scope:'col'},x)))),
      el('tbody',{},rows))):status('还没有已确认的样本，签入发现后自动重算。'));
}
export async function baseline(root,role) {
  const board=el('section',{class:'ledger'});
  const edit=el('section',{class:'action'});
  async function load(message) {
    const data=await mixer.baselines();
    const duty=data.versions.find(item=>item.code===data.on_duty);
    const ready=data.confirmed.normal>0&&data.confirmed.abnormal>0;
    board.replaceChildren(el('h2',{},'当班版本'),
      el('p',{},data.on_duty+' ',badge(baselineStates,'on_duty')),
      el('p',{class:'note'},'已确认样本：正常 '+data.confirmed.normal+' · 异常 '+data.confirmed.abnormal+
        (ready?'，样本齐了，可以启用候选。':'，正常和异常样本齐了才可启用候选。')),
      el('h2',{},'版本履历'),...data.versions.map(version=>
        el('details',{class:'clip'},el('summary',{},version.code+' ',badge(baselineStates,version.state)),
          el('p',{class:'note'},version.windows.length+' 个基线窗 · 登记人 '+version.created_by),
          el('p',{class:'mono'},version.windows.map(clip=>clip.code).join(' · ')))),
      el('h2',{},'候选与当班比对差异'),
      ...(data.comparisons.length?data.comparisons.map(comparisonView):[status('还没有候选基线。')]));
    edit.replaceChildren(el('h2',{},'基线换季'),message?status(message,'ok'):null);
    if (role!=='reviewer') {edit.append(status('复核员可登记候选、启用候选或回退到上一版。'));return;}
    const code=field('code','候选版本号',{placeholder:'BL-2026-B'});
    const sheet=el('textarea',{name:'windows',rows:'12',required:'',
      placeholder:'[{"code":"BL-2026-B-01","point":"MIC-PAV","slot":300,"frequency":730,"peak_share":0.2,"ring_index":0.24,"level":0.3,"self_echo":0.15}]'});
    const draft=el('button',{type:'button',class:'quiet',onclick:()=>{
      const prefix=(code.querySelector('input').value||'BL-NEW').toUpperCase();
      sheet.value=JSON.stringify(duty.windows.map((clip,index)=>({code:prefix+'-'+String(index+1).padStart(2,'0'),
        point:clip.point,slot:300+index*2,frequency:clip.frequency,peak_share:clip.peak_share,
        ring_index:clip.ring_index,level:clip.level,self_echo:clip.self_echo})),null,1);
    }},'以当班基线为底稿');
    edit.append(run('new-baseline',[code,el('label',{},el('span',{},'基线窗 JSON（至少 12 个）'),sheet),draft],
      '登记候选基线',async form=>{
        let windows;
        try {windows=JSON.parse(form.get('windows'));}
        catch {throw new Error('基线窗 JSON 无法解析');}
        const item=await mixer.createBaseline({code:form.get('code'),windows});
        await load(item.code+' 已登记为候选，比对已按当班基线计算。');
      }));
    const candidates=data.versions.filter(item=>item.state==='candidate');
    for (const item of candidates)
      edit.append(run('enable-'+item.code,[],'启用 '+item.code+' 为当班',async ()=>{
        const next=await mixer.enableBaseline(item.code,{expected:data.on_duty});
        await load(next.code+' 已启用为当班基线，其余候选已按新当班重算。');
      }));
    if (data.versions.some(item=>item.state==='retired'))
      edit.append(run('rollback',[],'回退到上一版',async ()=>{
        const next=await mixer.rollbackBaseline();
        await load('已回退到 '+next.code+'。');
      }));
  }
  root.replaceChildren(titleBlock('安澜 · 基线换季','声检基线版本','候选基线与当班基线用同一批已确认样本比对。启用或回退后，新诊断用当班基线，已排队的诊断与已开隔离单的复测沿用原版本。'),
    el('div',{class:'mix'},board,edit));
  await load();
}
