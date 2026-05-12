# nix/tui.nix — Hermes TUI (Ink/React) compiled with tsc and bundled
{ pkgs, hermesNpmLib, ... }:
let
  src = ../ui-tui;
  npmDeps = pkgs.fetchNpmDeps {
    inherit src;
    hash = "sha256-JuwShoVDzys7W350o4YQECWflOEsx2zLKlJq+zgGi7A=";
  };

  npm = hermesNpmLib.mkNpmPassthru { folder = "ui-tui"; attr = "tui"; pname = "sinoclaw-tui"; };

  packageJson = builtins.fromJSON (builtins.readFile (src + "/package.json"));
  version = packageJson.version;
in
pkgs.buildNpmPackage (npm // {
  pname = "sinoclaw-tui";
  inherit src npmDeps version;

  doCheck = false;
  npmFlags = [ "--legacy-peer-deps" ];

  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/sinoclaw-tui

    cp -r dist $out/lib/sinoclaw-tui/dist

    # runtime node_modules
    cp -r node_modules $out/lib/sinoclaw-tui/node_modules

    # @sinoclaw/ink is a file: dependency, we need to copy it in fr
    rm -f $out/lib/sinoclaw-tui/node_modules/@sinoclaw/ink
    cp -r packages/sinoclaw-ink $out/lib/sinoclaw-tui/node_modules/@sinoclaw/ink

    # package.json needed for "type": "module" resolution
    cp package.json $out/lib/sinoclaw-tui/

    runHook postInstall
  '';
})
