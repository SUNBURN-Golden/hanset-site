/* Client-side filters for the Drop-in & Sauna Finder. Cards are server-rendered; this only hides/shows them.
   State lives in the URL (?city=seoul&type=sauna&sunday=no&price=1&q=...) so Instagram links can deep-link. No storage, no network. */
(function(){
  var list=document.getElementById('finder-list'); if(!list) return;
  var cards=[].slice.call(list.querySelectorAll('.card'));
  var st={city:'',type:'',sub:'',sort:'',sunday:'',price:'',hyrox:'',cf:'',q:''};
  var qs=new URLSearchParams(location.search);
  ['city','type','sub','sort','sunday','price','hyrox','cf','q'].forEach(function(k){ if(qs.has(k)) st[k]=qs.get(k); });
  var $=function(id){return document.getElementById(id)};
  var home=cards.slice(), form=document.querySelector('.filters'), subEl=$('f-sub'), sortEl=$('f-sort'), q=$('f-q'), sun=$('f-sunday'), pr=$('f-price'), hy=$('f-hyrox'), cfc=$('f-cf'), cnt=$('f-count'), empty=$('f-empty');
  form.addEventListener('submit',function(e){e.preventDefault()});
  q.value=st.q; subEl.value=st.sub; sortEl.value=st.sort; sun.value=st.sunday; pr.checked=st.price==='1'; hy.checked=st.hyrox==='1'; cfc.checked=st.cf==='1';
  function seg(group){
    var btns=[].slice.call(document.querySelectorAll('[data-seg="'+group+'"] button'));
    btns.forEach(function(b){
      b.addEventListener('click',function(){ st[group]=b.getAttribute('data-v'); apply(); });
    });
    return btns;
  }
  var cityBtns=seg('city'), typeBtns=seg('type');
  q.addEventListener('input',function(){st.q=q.value.trim();apply()});
  subEl.addEventListener('change',function(){st.sub=subEl.value;apply()});
  sortEl.addEventListener('change',function(){st.sort=sortEl.value;apply()});
  sun.addEventListener('change',function(){st.sunday=sun.value;apply()});
  pr.addEventListener('change',function(){st.price=pr.checked?'1':'';apply()});
  hy.addEventListener('change',function(){st.hyrox=hy.checked?'1':'';apply()});
  cfc.addEventListener('change',function(){st.cf=cfc.checked?'1':'';apply()});
  [].forEach.call(document.querySelectorAll('[data-reset]'),function(b){b.addEventListener('click',function(){st={city:'',type:'',sub:'',sort:'',sunday:'',price:'',hyrox:'',cf:'',q:''};q.value='';subEl.value='';sortEl.value='';sun.value='';pr.checked=false;hy.checked=false;cfc.checked=false;apply()})});
  function sortCards(){
    var arr=home.slice();
    if(st.sort==='date') arr.sort(function(a,b){return a.dataset.date<b.dataset.date?1:a.dataset.date>b.dataset.date?-1:home.indexOf(a)-home.indexOf(b)});
    else if(st.sort==='name') arr.sort(function(a,b){return a.dataset.title.localeCompare(b.dataset.title,'ko')});
    arr.forEach(function(c){list.appendChild(c)});
  }
  function apply(){
    sortCards();
    var n=0, qq=st.q.toLowerCase();
    cards.forEach(function(c){
      var d=c.dataset, ok=true;
      if(st.city && d.city!==st.city) ok=false;
      if(st.type && d.type!==st.type) ok=false;
      if(st.sub && d.sub!==st.sub) ok=false;
      if(st.sunday && d.sunday!==st.sunday) ok=false;
      if(st.price==='1' && d.price!=='1') ok=false;
      if(st.hyrox==='1' && d.hyrox!=='1') ok=false;
      if(st.cf==='1' && d.cf!=='1') ok=false;
      if(qq && (d.name||'').indexOf(qq)<0) ok=false;
      c.hidden=!ok; if(ok) n++;
    });
    cityBtns.forEach(function(b){b.setAttribute('aria-pressed',String(b.getAttribute('data-v')===st.city))});
    typeBtns.forEach(function(b){b.setAttribute('aria-pressed',String(b.getAttribute('data-v')===st.type))});
    cnt.textContent='총 '+n+'곳';
    empty.hidden=n!==0;
    var p=new URLSearchParams(); Object.keys(st).forEach(function(k){ if(st[k]) p.set(k,st[k]); });
    var s=p.toString(); history.replaceState(null,'',location.pathname+(s?'?'+s:''));
  }
  apply();
})();
