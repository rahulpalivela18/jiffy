"""Headed smoke demo - opens a real browser window so you can watch the loop.

Run: python examples/watch_demo.py
No API keys needed; it exercises snapshot + executor only.
"""

import pathlib
import tempfile
import time

from jiffy.browser import Browser

HTML = """<!doctype html><html><body style="font-family:system-ui;padding:40px">
<h1>Job Application</h1>
<form onsubmit="document.title='SUBMITTED';return false">
  <p><label>First Name <input type="text" name="first" style="padding:6px"></label></p>
  <p><label>Email <input type="email" name="email" style="padding:6px"></label></p>
  <p><label>Resume <input type="file" name="resume"></label></p>
  <p><label>Country <select name="country">
     <option>United States</option><option value="IN">India</option></select></label></p>
  <p><button type="submit" style="padding:8px 16px">Submit Application</button></p>
</form>
<p id="out" style="color:#0a7"></p>
</body></html>"""


def find(actions, kind, needle):
    for a in actions:
        if a["kind"] == kind and needle.lower() in a["label"].lower():
            return a
    raise LookupError(f"no {kind} action matching {needle!r}")


def main():
    d = pathlib.Path(tempfile.mkdtemp())
    page_path = d / "form.html"
    page_path.write_text(HTML)
    resume = d / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake resume")

    browser = Browser(page_path.as_uri(), headless=False)
    try:
        def show(label):
            page = browser.observe(screenshot=False)
            print(f"\n=== {label} ===")
            for a in page["actions"]:
                if a["id"].startswith("e"):
                    print(f'  [{a["id"]}] {a["kind"]:<7} {a["role"]:<9} {a["label"]}')
            return page

        page = show("initial snapshot")
        time.sleep(1.5)

        browser.act(find(page["actions"], "fill", "First Name"), page, text="Rahul")
        page = show("typed First Name")
        time.sleep(1.5)

        browser.act(find(page["actions"], "upload", "Resume"), page, file_path=str(resume))
        page = show("uploaded resume.pdf")
        time.sleep(1.5)

        browser.act(find(page["actions"], "select", "Country"), page)
        page = show("selected India")
        time.sleep(1.5)

        browser.act(find(page["actions"], "click", "Submit"), page)
        page = show("clicked Submit")
        time.sleep(1.5)

        print("\nfinal:", browser.evaluate(
            "() => ({first:document.querySelector('[name=first]').value,"
            "country:document.querySelector('[name=country]').value,"
            "file:document.querySelector('[name=resume]').files[0]?.name,"
            "title:document.title})"))
        print("\nWatch demo complete. Closing in 2s.")
        time.sleep(2)
    finally:
        browser.close()


if __name__ == "__main__":
    main()
