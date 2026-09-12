const fs=require('fs'),path=require('path');
const esbuild=require('esbuild');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const root=input.sourceRoot;
const dynamicInputs = new Set();
function inlineDynamicAssets(source) {
  return source.replace(/(["'])(\/app\/frontend\/[^"']+\.css)\1/g, (_, quote, url) => {
    const file=root+'/products'+url;
    dynamicInputs.add(file);
    return JSON.stringify('data:text/css;base64,'+fs.readFileSync(file).toString('base64'));
  }).replace(/`\/app\/assets\/icons\/([^`]+)`/g, match => 'window.OHOffline.assetURL('+match+')');
}
function adaptTransport(source) {
  return inlineDynamicAssets(source)
    .replace(/\bwindow\.location\b/g, 'window.OHOffline.location')
    .replace(/(?<![\w.])location\b/g, 'window.OHOffline.location')
    .replace(/history\.(replaceState|pushState)\(/g, (_, method) => `window.OHOffline.${method}(`)
    .replace(/frame\.src = (`\/capability[^;]+);/g, 'frame.srcdoc = window.OHOffline.modulePage(moduleName, bridgeNonce);')
    .replace('frame.src = url.href;', 'frame.srcdoc = window.OHOffline.settingsPage(url);')
    .replace(/event\.origin !== window\.OHOffline\.location\.origin/g, 'event.origin !== window.OHOffline.receiveOrigin')
    .replace('new URL(link.href,', 'new URL(link.getAttribute("href"),')
    .replace(/},\s*window\.OHOffline\.location\.origin\s*\)/g, '}, window.OHOffline.messageOrigin)');
}
(async () => {
  const result = await esbuild.build({
    ...(input.entries ? { stdin: {
      contents: input.entries.map(file => `import ${JSON.stringify(file)};`).join('\n'),
      resolveDir: root, sourcefile: 'offline-page-entry.js',
    }} : { entryPoints: [input.entry] }),
    bundle: true, metafile: true, write: false,
    format: 'esm', target: 'es2022',
    plugins: [{ name: 'offline-transport', setup(build) {
      build.onResolve({ filter: /^\/app\/frontend\// }, args => ({ path: root + '/products' + args.path }));
      build.onLoad({ filter: /\.js$/ }, args => ({
        contents: adaptTransport(fs.readFileSync(args.path, 'utf8')),
        loader: 'js', resolveDir: path.dirname(args.path),
      }));
    }}],
  });
  fs.writeFileSync(path.join(__dirname, 'current-bundle-inputs.json'),
    JSON.stringify([...Object.keys(result.metafile.inputs).filter(name=>name!=='offline-page-entry.js').map(name => path.resolve(name)), ...dynamicInputs]));
  process.stdout.write('(async()=>{' + result.outputFiles[0].text.replace(/export\s*\{[^}]*\};?/g, '') + '})().catch(console.error);');
})().catch(error => { console.error(error); process.exitCode = 1; });
