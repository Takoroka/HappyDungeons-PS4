"""Adds the "PS4 (shadPS4)" box to the players page (/windurango/, server/web/index.html): a signed-in player makes or
sees their Happy Dungeons PS4 login there (the PS4 gateway's /ps4/whoami and /ps4/link, same site through Caddy).

  python3 players_page_ps4.py /opt/happydungeons/server/web/index.html [--dry-run]
  python3 players_page_ps4.py ... --remove [--dry-run]       take the box out again

Exact anchors (each must occur once), idempotent (marker "ps4_account_patch v1"), a backup index.html.before-ps4-<time>
next to the file, written in place (owner and mode stay). The page's own code is not changed. The box never asks for
a public shadNet password: the login is made for this server (random, or a new password the player picks for it).
"""
import hashlib
import os
import re
import shutil
import sys
import time

MARK = "ps4_account_patch v1"
TESTED_MD5 = "d649e577822cc563742344a0fda9b503"   # the live index.html this was built and tested against

CSS_ANCHOR = "  .note.bad { color: var(--danger); }\n"
CSS = """  /* ps4_account_patch v1 */
  .ps4Box [hidden] { display: none !important; }
  .ps4Box .ps4Opt { display: block; margin: 4px 0; }
  .ps4Box .ps4Own { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 0 24px; }
  .ps4Box .ps4Own .hint { flex-basis: 100%; margin: 0; }
  .ps4Box .ps4Login { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; margin: 0 0 6px; }
  .ps4Box .ps4Login code { font: inherit; font-weight: 600; user-select: all; overflow-wrap: anywhere; }
  .ps4Box #ps4Go { margin-top: 10px; }
  .ps4Box .ps4Steps { margin: 12px 0 0; }
  /* end ps4_account_patch v1 */
"""
HTML_ANCHOR = "  <!-- achievements_web_patch v1 -->\n"
HTML = """  <!-- ps4_account_patch v1 -->
  <section class="ps4Box" id="ps4Box" hidden>
    <h2>PS4 (shadPS4)</h2>
    <p class="hint">Play the PS4 version of Happy Dungeons in the shadPS4 emulator as this same player: same character,
       saves and friends. It signs in with its own login on this server, never with your public shadNet password.</p>
    <div id="ps4Linked" hidden>
      <p class="ps4Login">Account ID <code id="ps4Id"></code> Password <code id="ps4PwShown"></code>
        <button type="button" class="copy" id="ps4Reveal">Show</button>
        <button type="button" class="copy" id="ps4Change">Pick a new password</button></p>
    </div>
    <div id="ps4Make" hidden>
      <label class="ps4Opt"><input type="radio" name="ps4How" value="made" checked> Make a login for me (random
        password)</label>
      <label class="ps4Opt"><input type="radio" name="ps4How" value="own"> I'll pick the Account ID and a NEW password,
        only for Happy Dungeons</label>
      <div class="ps4Own" id="ps4Own" hidden>
        <input id="ps4NewId" placeholder="Account ID" maxlength="16" autocomplete="off">
        <input id="ps4NewPw" type="password" placeholder="New password (8+)" maxlength="128" autocomplete="new-password">
        <input id="ps4NewPw2" type="password" placeholder="Again" maxlength="128" autocomplete="new-password">
        <p class="hint">NOT the password of your public shadNet account or of anything else.</p>
      </div>
      <button type="button" class="primary" id="ps4Go">Make my PS4 login</button>
    </div>
    <p class="hint ps4Steps">In shadPS4: Settings &gt; Manage Users &gt; Create User, pick it, click ShadNet..., tick
       Enable ShadNet for this user and enter the Account ID and password. Click Set Default User on it before you play
       Happy Dungeons. Patches and the rest of the setup: <a href="/ps4/account">PS4 setup page</a>.</p>
    <p class="note" id="ps4Note" role="status"></p>
  </section>
  <!-- end ps4_account_patch v1 -->
"""
SCRIPT_ANCHOR = "</main>\n"
SCRIPT = """<script>
(() => {
  // ps4_account_patch v1 - the player's PS4 (shadPS4) login, made by the PS4 gateway (/ps4/*). Reads the key like the
  // achievements patch; the page's own script keeps it and cleans the address bar.
  const fragment = new URLSearchParams(location.hash.slice(1));
  const params = new URLSearchParams(location.search);
  let key = fragment.get("key") || params.get("key");
  if (!key) { try { key = localStorage.getItem("wdKey"); } catch (e) { /* storage blocked */ } }
  const $ = id => document.getElementById(id);
  if (!key || !$("ps4Box")) return;
  const [xuidBase, secret] = key.split(".");
  if (!xuidBase || !secret) return;
  let current = null;
  const note = (text, bad) => { $("ps4Note").textContent = text; $("ps4Note").className = "note" + (bad ? " bad" : ""); };
  const how = () => document.querySelector("input[name=ps4How]:checked").value;
  async function call(path, body) {
    const r = await fetch("/ps4/" + path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ xuidBase, secret }, body || {})), cache: "no-store" });
    let a = {};
    try { a = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok || !a.ok) throw new Error(a.reason || "the PS4 service answered " + r.status);
    return a;
  }
  function show(a) {
    current = a;
    $("ps4Box").hidden = false;
    $("ps4Linked").hidden = !a.linked;
    $("ps4Make").hidden = !!a.linked;
    $("ps4NewId").readOnly = false;
    $("ps4Go").textContent = "Make my PS4 login";
    if (!a.linked) return;
    $("ps4Id").textContent = a.npid;
    $("ps4PwShown").textContent = a.ownPassword ? "the one you picked" : "\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022\\u2022";
    $("ps4Reveal").hidden = !!a.ownPassword;
  }
  document.querySelectorAll("input[name=ps4How]").forEach(r => r.addEventListener("change", () => {
    $("ps4Own").hidden = how() !== "own";
  }));
  $("ps4Reveal").addEventListener("click", () => {
    if (current && current.password) { $("ps4PwShown").textContent = current.password; $("ps4Reveal").hidden = true; }
  });
  $("ps4Change").addEventListener("click", () => {
    $("ps4Make").hidden = false;
    document.querySelector("input[name=ps4How][value=own]").checked = true;
    $("ps4Own").hidden = false;
    $("ps4NewId").value = current.npid;
    $("ps4NewId").readOnly = true;
    $("ps4Go").textContent = "Set the new password";
    note("");
  });
  $("ps4Go").addEventListener("click", async () => {
    note("");
    $("ps4Go").disabled = true;
    try {
      let body = {};
      if (how() === "own") {
        const npid = $("ps4NewId").value.trim(), pw = $("ps4NewPw").value;
        if (!/^[A-Za-z][A-Za-z0-9_-]{2,15}$/.test(npid))
          throw new Error("Account IDs are 3-16 letters, digits, - or _, starting with a letter.");
        if (pw.length < 8) throw new Error("Pick a new password of at least 8 characters.");
        if (pw !== $("ps4NewPw2").value) throw new Error("The two passwords differ.");
        body = { npid: npid, password: pw };
      }
      const a = await call("link", body);
      $("ps4NewPw").value = $("ps4NewPw2").value = "";
      show(Object.assign(a, { linked: true }));
      note(a.ownPassword ? "Done: your PS4 login uses the password you picked." : "Done. Click Show to see the password.");
    } catch (e) {
      note(e.message, true);
    } finally {
      $("ps4Go").disabled = false;
    }
  });
  call("whoami").then(show).catch(() => { /* PS4 service not reachable, or not a player key: no box */ });
})();
</script>
"""
BLOCKS = [(CSS_ANCHOR, CSS, "after"), (HTML_ANCHOR, HTML, "before"), (SCRIPT_ANCHOR, SCRIPT, "after")]


def add(text):
    for anchor, block, where in BLOCKS:
        if text.count(anchor) != 1:
            sys.exit("anchor found %d times (need 1): %r" % (text.count(anchor), anchor))
        text = text.replace(anchor, anchor + block if where == "after" else block + anchor)
    return text


def remove(text):
    text = re.sub(r"  /\* ps4_account_patch v1 \*/\n.*?  /\* end ps4_account_patch v1 \*/\n", "", text, flags=re.S)
    text = re.sub(r"  <!-- ps4_account_patch v1 -->\n.*?  <!-- end ps4_account_patch v1 -->\n", "", text, flags=re.S)
    text = re.sub(r"<script>\n\(\(\) => \{\n  // ps4_account_patch v1.*?\n</script>\n", "", text, flags=re.S)
    return text


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry, undo = "--dry-run" in sys.argv, "--remove" in sys.argv
    if len(args) != 1:
        sys.exit(__doc__)
    path = args[0]
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    md5 = hashlib.md5(text.encode("utf-8")).hexdigest()
    print("%s: md5 %s%s" % (path, md5, " (the tested one)" if md5 == TESTED_MD5 else ""))
    present = MARK in text
    if undo:
        if not present:
            print("no PS4 box in it: nothing to do")
            return
        new = remove(text)
        if MARK in new:
            sys.exit("could not take every block out (edited by hand?): nothing written")
    else:
        if present:
            print("already has the PS4 box: nothing to do")
            return
        new = add(text)
    print("%s: %d -> %d bytes" % ("remove" if undo else "add", len(text), len(new)))
    if dry:
        print("dry run: nothing written")
        return
    backup = "%s.before-ps4-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    n = 2
    while os.path.exists(backup):   # never over an earlier backup
        backup = "%s.before-ps4-%s-%d" % (path, time.strftime("%Y%m%d-%H%M%S"), n)
        n += 1
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8", newline="") as f:   # in place: owner and mode stay
        f.write(new)
    print("written; backup %s" % backup)


if __name__ == "__main__":
    main()
