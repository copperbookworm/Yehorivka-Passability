# Third-party notices

The MIT license in [LICENSE](LICENSE) covers the source code authored for this
project (the Android WebView shell, the single-page map engine, the build and
annotation tooling).

The following third-party material is **not** covered by that license:

## Squad / map imagery

*Squad* and its maps, terrain and game assets are the property of
Offworld Industries. This project is an unofficial fan-made tool and is not
affiliated with or endorsed by Offworld Industries.

## SquadCalc map data

The per-map elevation, basemap, topomap and 3D instance data used to generate
`android/YehorivkaMap/app/src/main/assets/maps/*.html` are downloaded from
[SquadCalc](https://squadcalc.app) by `tools/download_maps.py`. They remain
the property of their respective authors.

Because of that, if you redistribute a build of this app you are responsible
for checking that you have the right to redistribute the embedded map data.
The code alone can be reused freely under the MIT license.
