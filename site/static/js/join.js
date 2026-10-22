/* /join/: show the error line when the OAuth flow bounces back with ?error=1. No inline script (CSP), no network. */
(function(){
  if(new URLSearchParams(window.location.search).get('error')==='1'){
    var el=document.getElementById('join-error');
    if(el) el.hidden=false;
  }
})();
