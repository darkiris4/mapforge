# NGA-controlled data and PKI endpoints

MapForge offers two ways to bring NGA and other controlled data into a package:

1. **Local library.** Download products through NGA's portals with your CAC, then let MapForge
   index them from disk. This works everywhere, including air-gapped systems, and is the
   recommended path.
2. **PKI-protected service endpoints.** Connect a WMS, WMTS or ArcGIS ImageServer that you already
   reach with a certificate (for example NGA GEGD web services) and pull only the area you draw.

> **Handling.** Only install MapForge, and only process controlled data, on systems authorised
> for that data's classification and distribution statement. Output packages inherit the
> markings and distribution statements (for example Distribution C/D, ITAR/EAR) of their inputs.
> Label and store them accordingly. MapForge does not add or track markings for you. The
> `README.txt` in each package lists the inputs so you can carry the markings forward.

> **Status: untested.** This entire document is theoretical. None of it — the local-library
> NGA product handling, the GEGD WMTS/WMS presets, PKI client-certificate auth, the DoD CA
> bundle steps, or the PKCS#11 proxy approach in section 3 — has been exercised against a real
> NGA account, GEGD endpoint, or CAC. Treat it as a starting point to validate against an actual
> account and credentials, not as a verified procedure.

---

## 1. Local library (CADRG, CIB, ECRG, DTED, NITF, GeoTIFF)

1. On a CAC-enabled workstation, download what you need from the NGA portal your program uses:
   CADRG (ONC/TPC/JOG/JNC/GNC/TLM), CIB (1/5/10 m), ECRG, DTED Level 0/1/2, VMap-derived
   rasters, NITF imagery and so on. Keep each product's folder structure intact. CADRG and CIB
   are read through their `A.TOC` file, and ECRG through `TOC.xml`.
2. Put the data in a library directory on the MapForge host:
   * copy it into `$MAPFORGE_LIBRARY` (default `data/library`), or
   * point MapForge at the media where it already lives: `--library /mnt/nga --library /mnt/dted`,
     or `MAPFORGE_LIBRARY=/mnt/nga:/mnt/dted`, or
   * use **Local library → Upload archive / file** in the UI. Zips and tarballs are extracted
     into `<library>/uploads/`.
3. Click **Rescan**. MapForge groups the data into products, such as `CADRG ONC 1:1M`,
   `CIB 5M` and `DTED Level 2`, plus one product per top-level folder of loose GeoTIFF/NITF
   files.
4. On the Build tab, the products appear under **Local library — NGA / on-disk**. They behave
   like every other layer: bound, mosaic, reproject and convert (DTED output works from any
   elevation product).

Tips:

* Mixed-scale CADRG on one disc shows up as separate products per series and scale, which is
  what you want for scale-dependent display.
* DTED you already have is re-cut to exact cells for your box. Enable DTED output on the
  elevation layer.
* Read-only mounts are fine. MapForge never writes into library directories except `uploads/`.

---

## 2. PKI-protected WMS / WMTS / ImageServer endpoints

Open **Endpoints / NGA** in the UI and pick a preset (NGA GEGD WMTS, NGA GEGD WMS, or a controlled
ArcGIS ImageServer). Then:

* **URL:** the service URL shown in your provider account. For GEGD, look in your account or
  profile area for the web-service (WMS/WMTS) connection URLs. MapForge doesn't ship any GEGD
  URLs because they depend on your account and profile.
  * WMTS: the `GetCapabilities` URL, plus the layer name (and optionally the tile matrix set).
  * WMS: the base `GetMap` endpoint, plus the layer name.
  * ArcGIS: the URL ending in `/ImageServer`.
* **Auth → PKI client certificate:** paths *on the MapForge server* to a PEM certificate and a
  PEM private key, the key password if any, and a CA bundle that trusts the server.
* Click **Test connection**. It opens the service for a tiny area (your drawn box if you have
  one) and reports the size, bands and CRS, or the exact error. Then click **Save**. The
  endpoint becomes a layer on the Build tab.

Credentials are stored in `$MAPFORGE_DATA/config/endpoints.json` on the server (passwords are
masked in the UI). Restrict that directory, for example `chmod 700 data/config`.

### Converting a soft certificate (.p12 / .pfx) to PEM

```bash
# certificate (client cert only)
openssl pkcs12 -in me.p12 -clcerts -nokeys -out me.crt.pem
# private key, encrypted with a passphrase you then enter in MapForge as "Key password"
openssl pkcs12 -in me.p12 -nocerts -out me.key.pem
# (OpenSSL 3 and old .p12 files may need:  -legacy)
chmod 600 me.key.pem
```

### DoD / enterprise CA bundle

DoD sites chain to the DoD Root CAs, which aren't in standard OS trust stores. Get the DoD PKI CA
certificate bundle from DoD Cyber Exchange (the PKI/PKE "certificates" downloads, which are also
what InstallRoot installs), or ask your organisation's PKI office. Convert it to a single PEM file
if it comes as PKCS#7:

```bash
openssl pkcs7 -print_certs -inform DER -in Certificates_PKCS7_DoD.der.p7b -out dod-ca-bundle.pem
# (use -inform PEM if the file is PEM-encoded)
```

Then enter that file as the endpoint's **CA bundle**. In Docker, mount the certificates
read-only (`./pki:/pki:ro` in `docker-compose.yml`) and use `/pki/...` paths.

---

## 3. CAC / hardware tokens

A CAC's private key **cannot be exported**, by design. MapForge talks to services through GDAL
and libcurl, which need a key file, so there is no way to point MapForge straight at a smart
card. Options, from simplest to most involved:

1. **Use the local library path** (section 1). Download with your CAC in the browser as usual,
   then process offline. This covers most NGA products and needs no special setup.
2. **Use an organisational soft certificate or service account** where your program issues one
   for machine-to-machine access (common for web-service access to imagery programs). Then
   follow section 2.
3. **Run a local PKCS#11-aware TLS forwarding proxy** on the MapForge host, and point the
   endpoint URL at the proxy (for example `http://127.0.0.1:9443/...`, auth = None). The proxy
   performs mutual TLS to the real server using the card through its PKCS#11 module (OpenSC
   `opensc-pkcs11.so`, or your middleware's module). Generic building blocks:
   * **stunnel** in client mode with an OpenSSL PKCS#11 engine, or an OpenSSL 3 PKCS#11
     provider, configured with the certificate/key as `pkcs11:` URIs.
   * **nginx** or **HAProxy** built against OpenSSL 3 with a PKCS#11 provider, acting as a
     reverse proxy with an upstream client certificate.

   Exact configuration depends on your OS, middleware, OpenSSL version and local policy, and
   the card must stay inserted and unlocked (PIN) while jobs run. Treat this as an
   engineering task to clear with your ISSM/ISSO. **Only do this if local policy allows
   automated use of your credential.** Many policies prohibit unattended use of a CAC.

---

## Troubleshooting

| Symptom (from **Test connection**) | Likely cause |
|---|---|
| `SSL certificate problem: unable to get local issuer certificate` | Missing or wrong **CA bundle**. Use the DoD root bundle. |
| `could not load PEM client certificate` / `unable to set private key file` | Wrong path, wrong format (DER/P12 instead of PEM), or wrong key password. |
| HTTP 401/403 | The certificate isn't authorised for that service or layer, or the account/profile is wrong. |
| WMTS opens but the layer is blank | Wrong layer / tile-matrix-set, or no coverage in the test box. Draw a box where you know data exists and test again. |
| Works in the browser but not in MapForge | The browser is using the CAC. See section 3. |
