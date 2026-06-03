# Pegasus Resolver

![Python](https://img.shields.io/badge/python-3.10%2B-24292f)
![Pegasus DL](https://img.shields.io/badge/for-Pegasus%20DL-24292f)

Provider link resolver for [Pegasus DL](https://github.com/pegasus-ps5/pegasus-dl).

Pegasus Resolver runs on a computer on your local network. When Pegasus DL sees
a catalog link that opens a provider page instead of a direct package file, the
resolver helps turn that page into a downloadable URL the PS5 can queue.

## When To Use It

Use Pegasus Resolver when a package link needs an extra step before the download
starts. You do not need it for links that are already directly downloadable.

Pegasus DL still handles the library, destination folder, queue, progress, and
download itself. Pegasus Resolver only helps with supported provider links.

## Supported Providers

| Provider | Hosts |
| --- | --- |
| AkiraBox | `akirabox.com`, `akirabox.to` |
| BuzzHeavier | `buzzheavier.com`, `bzzhr.co`, `bzzhr.to` |
| DataNodes | `datanodes.to` |
| VikingFile | `vik1ngfile.site`, `vikingfile.com` |

## Install

```sh
git clone https://github.com/pegasus-ps5/pegasus-resolver.git
cd pegasus-resolver
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

## Run

Start the resolver on the machine that will handle provider pages:

```sh
pegasus-resolver --host 0.0.0.0 --port 7799
```

Then open Pegasus DL, go to Settings, and set the Resolver URL to that machine:

```text
http://<your-computer-ip>:7799
```

After saving, queue packages normally from Pegasus DL. Supported links will use
the resolver when needed, and direct links will download without it.

## Check It

Open this URL in a browser or terminal:

```text
http://127.0.0.1:7799/api/health
```

You should see a small JSON response with `status` set to `ok`.

## Development

Install the development extras:

```sh
python -m pip install -e ".[dev]"
```

Run the test suite:

```sh
python -m pytest
```

## Scope

Pegasus Resolver does not provide catalogs, package links, account bypasses, PSN
spoofing, anti-cheat bypasses, or content unlocking.

Use it only with content you own or have permission to download.
