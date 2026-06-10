"""py2app build config — produces FounderEnrich.app from app.py.

Build locally:    python founder_enrich/setup.py py2app
Build in CI:      see .github/workflows/release-enricher.yml

The .app is unsigned by default. First-launch will trip macOS Gatekeeper;
teammates right-click → Open the first time to bypass.
"""
from setuptools import setup

APP = ["founder_enrich/app.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "FounderEnrich",
        "CFBundleDisplayName": "FounderEnrich",
        "CFBundleIdentifier": "com.tokentape.founderenrich",
        "CFBundleVersion": "0.3.7",
        "CFBundleShortVersionString": "0.3.7",
        "LSUIElement": True,  # menu bar only, no Dock icon
        "NSHighResolutionCapable": True,
    },
    "packages": ["founder_enrich", "rumps", "requests", "bs4", "dns", "exa_py"],
    "includes": ["founder_enrich.discover", "founder_enrich.resolve",
                 "founder_enrich.verify", "founder_enrich.pipeline",
                 "founder_enrich.cli", "founder_enrich.exa",
                 "founder_enrich.cache"],
}

setup(
    app=APP,
    name="FounderEnrich",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
