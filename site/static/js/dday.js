/* D-day labels computed in Korea time from data-date="YYYY-MM-DD". No network. */
(function(){
  var now=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Seoul'}));
  var today=new Date(now.getFullYear(),now.getMonth(),now.getDate());
  [].forEach.call(document.querySelectorAll('[data-dday]'),function(el){
    var p=el.getAttribute('data-dday').split('-'); var t=new Date(+p[0],+p[1]-1,+p[2]);
    var d=Math.round((t-today)/864e5), out;
    if(d>0) out='D-'+d; else if(d===0) out='D-DAY'; else return;
    var b=el.querySelector('b'); if(b) b.textContent=out; else el.textContent=out;
  });
})();
/* Hide event cards whose last day has passed (page may be older than today). */
(function(){
  var now=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Seoul'}));
  var today=new Date(now.getFullYear(),now.getMonth(),now.getDate());
  [].forEach.call(document.querySelectorAll('[data-end]'),function(el){
    var p=el.getAttribute('data-end').split('-'); var t=new Date(+p[0],+p[1]-1,+p[2]);
    if(t<today) el.hidden=true;
  });
})();
