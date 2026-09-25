from maimai_bot.services.lxns_assets import _challenge_cookie_header


def test_parses_lxns_numeric_cookie_challenge() -> None:
    script = """
    <script>
    function n(){t+='EO_Bot_Ssid=';t=a(t,2774990848);}
    var e={WTKkN:1589993180,bOYDu:255178234,dtzqS:function(a,n){return a+n},
    wyeCN:606535,pCQRM:function(a){return a()}},t=0;
    </script>
    """

    assert _challenge_cookie_header(script) == ("__tst_status=1845777949#; EO_Bot_Ssid=2774990848")
