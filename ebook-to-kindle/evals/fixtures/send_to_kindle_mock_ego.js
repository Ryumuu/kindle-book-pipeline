// Offline harness for send_to_kindle.py: runs the generated ego-browser script in a Node VM
// against a fake Send to Kindle / Manage Content page. No browser, network or Amazon account.
// The fake library list is newest first, like the real one.
// Usage: node send_to_kindle_mock_ego.js <deliver-script.js> <upload-only-script.js>  -> one JSON line per case.
const vm = require('node:vm');
const fs = require('node:fs');
const script = fs.readFileSync(process.argv[2], 'utf8');

async function runCase(name, cfg={}) {
  const state = {externalUpload:false,selectionValidated:false,postReads:0,pdocsReads:0,baselineReads:0,baselineObserved:[],keeps:[],pdocsVisits:0, page:'', uploaded:false, dialogOpen:false,
    loaded:false, time:0, delivered:[], events:[], results:[], finishes:0};
  const names=cfg.names || ['Paperwhite', 'Second Kindle'];
  const boxes=names.map((name,i)=>({id:'deliver_to_device_list_NEW_'+i, checked:!!(cfg.selected||[])[i], name}));
  let currentAsin='NEW';
  function rows(){
    state.pdocsReads++;
    const values=state.pdocsVisits===1 ? (cfg.beforeSeries ? cfg.beforeSeries[Math.min(state.baselineReads++,cfg.beforeSeries.length-1)] : (cfg.before===undefined ? [{asin:'OLD',title:'Dune'}] : cfg.before))
      : (cfg.afterSeries ? cfg.afterSeries[Math.min(state.postReads++,cfg.afterSeries.length-1)] : (state.externalUpload ? [{asin:'EXTERNAL',title:'Dune'},{asin:'OLD',title:'Dune'}] : (cfg.after || [{asin:'NEW',title:'Dune'},{asin:'OLD',title:'Dune'}])));
    if(state.pdocsVisits===1)state.baselineObserved.push(values.map(r=>r.asin));
    return values.map(r=>({id:r.asin+':KindlePDoc',querySelector:()=>({innerText:r.title})}));
  }
  const cancel={click(){state.dialogOpen=false;state.events.push('cancel')}};
  const confirm={id:'ACTION_CONFIRM', disabled:!!cfg.disabled,className:'',getAttribute:()=>null,
    click(){state.delivered.push({asin:currentAsin,devices:boxes.filter(b=>b.checked).map(b=>b.name)});
      state.events.push('confirm'); if(!cfg.stillOpen)state.dialogOpen=false;}};
  const dialog={querySelector:q=>q.includes('CONFIRM')?confirm:cancel,get offsetParent(){return state.dialogOpen ? {}:null;}};
  function setAsin(asin){currentAsin=asin;boxes.forEach((b,i)=>{b.id='deliver_to_device_list_'+asin+'_'+i;});}
  const trigger={click(){state.dialogOpen=true;state.loaded=!cfg.asyncDevices;state.events.push('open-dialog');}};
  const document={
    body:{get innerText(){return cfg.localRejected ? 'Upload failed' : cfg.japanese ? 'アップロード済み' : 'Your file is on its way';}},
    querySelector(q){if(q.includes('nav-link'))return {innerText:'Test account'};if(q.includes('password'))return cfg.login ? {} : null;if(q.includes('_CANCEL'))return cancel;return null;},
    querySelectorAll(q){
      if(q.includes('KindlePDoc'))return rows();
      if(q.endsWith(' li')) return state.loaded ? boxes.map(b=>({querySelector:q=>q.includes('action_list')?{innerText:b.name}:b,innerText:b.name})):[];
      if(q.includes('input[type="checkbox"]'))return boxes;
      throw Error('Unexpected selector: '+q);
    },
    getElementById(id){
      if(id==='s2k-r2s-send-button')return {disabled:false,offsetParent:{}};
      if(id.startsWith('DELIVER_OR_REMOVE_FROM_DEVICE_DIALOG_')){setAsin(id.slice('DELIVER_OR_REMOVE_FROM_DEVICE_DIALOG_'.length));return dialog;}
      if(id==='ACTION')return trigger;
      const b=boxes.find(b=>id===b.id || id===b.id+'_checkmark');
      if(b)return {click(){b.checked=!b.checked;}};
      return null;
    }
  };
  const page=vm.createContext({document,window:{location:{href:'https://www.amazon.co.jp/sendtokindle'}}});
  const context=vm.createContext({
    useOrCreateTaskSpace:async()=>{if(cfg.taskError)throw Error('synthetic task creation failure');return {id:'review'};},
    completeTaskSpace:async(id,opts)=>{state.finishes++;state.keeps.push(opts.keep);if(cfg.cleanupError)throw Error('synthetic cleanup failure');},
    openOrReuseTab:async()=>{},
    gotoAndWait:async url=>{state.page=url;if(cfg.concurrentUpload && state.pdocsVisits===1 && url.includes('/sendtokindle')){state.externalUpload=true;state.events.push('external-Dune-upload-visible-before-local-send');}if(url.includes('/pdocs/')){state.pdocsVisits++;if(cfg.deliveryNavigationError && state.uploaded)throw Error('synthetic pdocs timeout');}},
    wait:async t=>{state.time+=t;if(cfg.afterConfirmWaitError && state.delivered.length)throw Error('synthetic post-confirm wait failure');if(state.dialogOpen && !cfg.neverDevices)state.loaded=true;},
    js:async source=>{
      // Page mutations happen between separate remote calls, after validation has returned.
      if(source.includes('btn.click()') && state.selectionValidated){
        if(cfg.selectionReset){boxes.forEach((b,i)=>b.checked=i===1);state.events.push('page-reset-selection-to-second-device');}
        if(cfg.extraSelection){boxes[1].checked=true;state.events.push('page-selected-additional-device');}
        if(cfg.disabledBeforeClick){confirm.disabled=true;state.events.push('page-disabled-confirm');}
        if(cfg.beforeClickThrows)throw Error('synthetic RPC failure before confirm execution');
      }
      const value=vm.runInContext(source,page);
      if(source.includes('const selected = boxes.filter') && !source.includes('btn.click()') && value && value.ok){state.selectionValidated=true;state.events.push('validated-selection');}
      if(source.includes('return !!(d && d.offsetParent')){
        if(cfg.statusReadNull){state.events.push('dialog-status-response-null');return null;}
        if(cfg.statusReadUndefined){state.events.push('dialog-status-response-undefined');return undefined;}
        if(cfg.statusReadThrows)throw Error('synthetic dialog status read failure');
      }
      if(cfg.confirmResponseLost && source.includes('btn.click()') && state.delivered.length)throw Error('synthetic RPC response lost after confirm click');if(cfg.confirmResponseNull && source.includes('btn.click()') && state.delivered.length)return null;return value;},
    click:async id=>{state.events.push(id);if(id==='#s2k-r2s-send-button'){state.uploaded=true;if(cfg.localRejected)state.events.push('local-upload-rejected');if(cfg.sendResponseLost)throw Error('synthetic RPC response lost after send click');}},
    cdp:async()=>{if(cfg.prepareError)throw Error('synthetic CDP failure');return {};},drainEvents:async()=>[{method:'Page.fileChooserOpened',params:{backendNodeId:1}}],
    setInterval:fn=>{Promise.resolve().then(fn);return 1;},clearInterval:()=>{},setTimeout:()=>1,
    cliLog:line=>state.results.push(JSON.parse(line))
  });
  await vm.runInContext(cfg.cloudOnly ? fs.readFileSync(process.argv[3],'utf8') : script,context);
  return {name,uploaded:state.uploaded,delivered:state.delivered,results:state.results,
    pdocsVisits:state.pdocsVisits,pdocsReads:state.pdocsReads,finishes:state.finishes,keeps:state.keeps,baselineObserved:state.baselineObserved,dialogOpen:state.dialogOpen,simulatedWaitSeconds:state.time,events:state.events};
}
(async()=>{
  const cases=[
    ['happy',{}],
    ['device-selection-reset-between-rpcs',{selectionReset:true}],
    ['extra-device-selected-between-rpcs',{extraSelection:true}],
    ['confirm-disabled-between-rpcs',{disabledBeforeClick:true}],
    ['confirm-rpc-fails-before-execution',{beforeClickThrows:true}],
    ['dialog-status-null-open',{stillOpen:true,statusReadNull:true}],
    ['dialog-status-undefined-open',{stillOpen:true,statusReadUndefined:true}],
    ['dialog-status-throws-open',{stillOpen:true,statusReadThrows:true}],
    ['concurrent-upload-before-local-send',{concurrentUpload:true}],
    ['concurrent-upload-japanese-fallback',{concurrentUpload:true,japanese:true}],
    ['concurrent-upload-japanese-cloud-only',{concurrentUpload:true,japanese:true,cloudOnly:true}],
    ['concurrent-upload-rejected-cloud-only',{concurrentUpload:true,localRejected:true,cloudOnly:true}],
    ['anchor-missing',{after:[{asin:'NEW',title:'Dune'}]}],
    ['anchor-missing-japanese',{after:[{asin:'NEW',title:'Dune'}],japanese:true}],
    ['anchor-appears-later',{afterSeries:[[{asin:'NEW',title:'Dune'}],[{asin:'NEW',title:'Dune'},{asin:'OLD',title:'Dune'}]]}],
    ['seen-union-excludes-earlier-head',{beforeSeries:[[{asin:'EARLY',title:'Dune'},{asin:'OLD',title:'Dune'}],[{asin:'OLD',title:'Dune'}],[{asin:'OLD',title:'Dune'}]],after:[{asin:'EARLY',title:'Dune'},{asin:'OLD',title:'Dune'}]}],
    ['baseline-sequence-keeps-reordering',{beforeSeries:Array.from({length:8},(_,i)=>i%2?[{asin:'A',title:'Other'},{asin:'B',title:'Other'}]:[{asin:'B',title:'Other'},{asin:'A',title:'Other'}])}],
    ['top-row-arrives-late-assumption-violation',{before:[{asin:'OTHER',title:'Other'}],after:[{asin:'PREEXISTING_TOP',title:'Dune'},{asin:'OTHER',title:'Other'}]}],
    ['ambiguous-japanese-fallback',{japanese:true,after:[{asin:'NEW',title:'Dune'},{asin:'NEW2',title:'Dune'},{asin:'OLD',title:'Dune'}]}],
    ['empty-baseline-unrecognized-cloud-only',{before:[],cloudOnly:true,japanese:true}],
    ['nonempty-incomplete-baseline-old-only',{before:[{asin:'OTHER',title:'Other book'}],after:[{asin:'OTHER',title:'Other book'},{asin:'OLD',title:'Dune'}]}],
    ['same-count-different-asins-old-only',{beforeSeries:[[{asin:'OLD',title:'Dune'}],[{asin:'OTHER',title:'Other book'}]],after:[{asin:'OTHER',title:'Other book'},{asin:'OLD',title:'Dune'}]}],
    ['nonempty-incomplete-japanese-fallback',{japanese:true,before:[{asin:'OTHER',title:'Other book'}],after:[{asin:'OTHER',title:'Other book'},{asin:'OLD',title:'Dune'}]}],
    ['confirm-click-response-lost',{confirmResponseLost:true}],
    ['confirm-click-response-null',{confirmResponseNull:true}],
    ['post-confirm-wait-error',{afterConfirmWaitError:true}],
    ['send-click-response-lost',{sendResponseLost:true}],
    ['prepare-error',{prepareError:true}],
    ['task-creation-error',{taskError:true}],
    ['login-required',{login:true}],
    ['login-required-cleanup-error',{login:true,cleanupError:true}],
    ['empty-baseline-japanese',{before:[],japanese:true}],
    ['empty-baseline-cloud-only',{before:[],cloudOnly:true}],
    ['unstable-nonempty-baseline',{beforeSeries:Array.from({length:8},(_,i)=>Array.from({length:i+1},(_,j)=>({asin:'OLD'+j,title:'Other'})))}],
    ['device-list-never-loads',{asyncDevices:true,neverDevices:true}],
    ['empty-baseline-old-only',{before:[],after:[{asin:'OLD',title:'Dune'}]}],
    ['complete-baseline-old-only',{after:[{asin:'OLD',title:'Dune'}]}],
    ['ambiguous-new-documents',{after:[{asin:'NEW',title:'Dune'},{asin:'NEW2',title:'Dune'},{asin:'OLD',title:'Dune'}]}],
    ['new-copy-below-newest-old-document',{after:[{asin:'OLD',title:'Dune'},{asin:'LATE',title:'Dune'}]}],
    ['async-device-list',{asyncDevices:true}],
    ['ambiguous-devices',{names:['Paperwhite Old','Paperwhite New']}],
    ['clear-other-selected-device',{selected:[false,true]}],
    ['disabled-confirm',{disabled:true}],
    ['dialog-stays-open',{stillOpen:true}],
    ['post-upload-navigation-error',{deliveryNavigationError:true}],
    ['post-delivery-cleanup-error',{cleanupError:true}],
    ['japanese-upload-fallback',{japanese:true}]
  ];
  for(const [name,cfg] of cases) console.log(JSON.stringify(await runCase(name,cfg)));
})().catch(e=>{console.error(e);process.exitCode=1;});
