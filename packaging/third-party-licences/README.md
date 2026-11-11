# Licence texts for code that wheels link in statically

`scripts/collect_licences.py` copies a distribution's own licence files from its metadata. Some
wheels also link other projects into their extension module without shipping those texts; they
live here, one folder per distribution, and `STATIC_LINKED` in the script names each component.

## curl_cffi (checked against 0.16.3, 2026-11-08)

`curl_cffi/_wrapper.abi3.so` is curl-impersonate (libcurl 8.21.0-IMPERSONATE). Its build paths
(`strings _wrapper.abi3.so | grep -o 'build/deps/src/[a-z0-9_.-]*' | sort -u`) name: boringssl,
curl, libidn2, ngtcp2, nghttp3, nghttp2, zstd, brotli, zlib. The texts were taken from each
project's upstream repository on 2026-11-08.

When curl_cffi is upgraded, re-run that `strings` check and update this folder and
`STATIC_LINKED` (the collector warns when the installed version differs from the checked one).
