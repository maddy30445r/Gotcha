# Tauri + Vanilla

This template should help get you started developing with Tauri in vanilla HTML, CSS and Javascript.

## Recommended IDE Setup

- [VS Code](https://code.visualstudio.com/) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer)

## Troubleshooting

- **An old/ghost Gotcha keeps opening from a `gotcha://` link after you deleted the app?**
  macOS Launch Services still has stale deep-link registrations (build artifacts under
  `target/`, old DMG mounts). Run `./clean-deeplink.sh` to remove the artifacts and flush
  the registrations. Re-run it after a `./build-dmg.sh` if the wrong build starts launching.
