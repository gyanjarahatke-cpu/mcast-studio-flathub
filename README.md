# MCast Studio Flatpak packaging

This independent public repository contains packaging tools and desktop metadata for MCast Studio. Application source remains in its private repository. No existing repository needs a visibility change. Windows distribution remains Microsoft Store only; do not upload Windows installers here.

Current candidate: **1.0.0-beta.1**. Application ID: **com.mcaststudio.MCast**. Packaging targets Linux x86_64 with GNOME runtime 50. This repository does not claim a published Flathub app or a verified Flatpak runtime yet.

## Prepare a candidate

After the remaining application changes, produce and verify the complete self-contained Linux x64 Release output using the application's normal native build and publish process. The archive must contain the contents of that output at its root, including MCast, native libraries, .NET runtime, Browser, Resources, and Tools. Preserve executable bits and relative library links. Do not include source, credentials, development data, or debug symbols.

Compile native code and publish the managed application against the selected GNOME SDK. The application's canonical Release output can be mounted into that build environment. A build on a newer host distribution can import libc symbols unavailable in the Flatpak runtime. Verify ELF dependencies and launch the installed package inside its declared runtime before publishing the archive.

Publish that Linux archive to an upstream HTTPS release location. Record its SHA-256 and a real screenshot of the Linux application. The packaging workflow requires these actual inputs; it does not contain a fake binary URL or checksum.

Use **Actions ? Build Flatpak candidate ? Run workflow** with the archive URL, SHA-256, version, release date, and screenshot URL. The workflow verifies the archive, generates the manifest and AppStream metadata, builds a Flatpak candidate, and uploads artifacts. Every workflow is manual; pushing this setup starts no build. No private-repository token is required. It does not submit to Flathub or create releases automatically.

For local preparation, download the same release archive and run:

```sh
python3 scripts/prepare_release.py --archive "$ARCHIVE" --url "$RELEASE_URL" --sha256 "$SHA256" --version 1.0.0-beta.1 --date "$RELEASE_DATE" --screenshot-url "$SCREENSHOT_URL"
flatpak-builder --user --install-deps-from=flathub --repo=repo build generated/com.mcaststudio.MCast.json
flatpak build-bundle repo MCastStudio.flatpak com.mcaststudio.MCast beta
```

The generated directory is the standalone packaging input. It contains only the manifest, launcher, icon, desktop entry, metadata, and flathub.json; it references the verified public archive rather than copying the binary into Git.

For installation testing before the release archive is public, use the same generator with `--local-archive` instead of `--url`:

```sh
python3 scripts/prepare_release.py --local-archive --archive "$ARCHIVE" --sha256 "$SHA256" --version 1.0.0-beta.1 --date "$RELEASE_DATE"
flatpak-builder --user --install-deps-from=flathub --repo=repo build generated/com.mcaststudio.MCast.json
flatpak build-bundle repo MCastStudio.flatpak com.mcaststudio.MCast beta --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user MCastStudio.flatpak
```

This produces the same application package, launcher, and sandbox permissions using a verified local archive. It does not publish a release or supply invented URLs. A screenshot is optional only for this local installation candidate; the public release workflow still requires a real screenshot. Generated local manifests contain a machine path and must not be committed or submitted to Flathub.

## Before Flathub submission

Flathub currently does not accept new beta-only applications. Keep this v1 beta as an upstream candidate. A human maintainer must review the [requirements](https://docs.flathub.org/docs/for-app-authors/requirements) and follow the [submission process](https://docs.flathub.org/docs/for-app-authors/submission) once a stable version has been built, installed, and exercised in the sandbox. Use the current supported runtime at submission time.

Flathub's current policy requires disclosure of AI-generated application or packaging material and its approximate extent. The initial packaging automation, tests, workflow, metadata, and this documentation were prepared with AI assistance; the icon is an existing MCast asset. The private application has also received AI-assisted changes, whose extent the application maintainer must review. An AI agent must not open or automate the Flathub submission PR or generate its submission/review communication. This repository deliberately contains no submission bot or submission PR text template.

Validate the manifest with the Flathub linter and metadata with AppStream; supply real Linux screenshots and verify domain ownership for mcaststudio.com. Review the commercial license and redistribution terms before making a binary publicly available. An MCast account and valid application license are required.

## Sandbox verification

The launcher executes the packaged application in `/app/lib/mcast`. Application data follows the runtime-provided XDG data location and the application's existing MCast directory. No second settings store or host escape is introduced.

Permissions cover network streaming, X11/Wayland display, PulseAudio/PipeWire audio, device access for cameras/GPU/capture hardware, selected media directories, Secret Service for protected credentials, and the Avahi system service for native network-device discovery. The Avahi permission grants access to that named service only; the host Avahi service must be available for network discovery. Device access is broad because the native capture path uses V4L2 and optional capture devices; this must be justified during review. No host filesystem, unrestricted bus, host-spawn, or sandbox-disable permission is granted.

Verify launch, license sign-in and browser return, file pickers, camera/audio/screen capture, CEF, GPU preview/program, recording/streaming, shutdown, and data persistence under Flatpak. Kernel virtual-camera modules and vendor hardware drivers are host prerequisites; their sandbox availability is unverified. Do not label these integrations supported until exercised. The candidate builder is not hardware or Store certification.

## Packaging checks

```sh
python3 -m unittest discover -s tests -v
```

These checks validate packaging inputs and generated files. They do not launch or build MCast. The `Check packaging tools` workflow is also manual.
