// Mocha entry point that @vscode/test-electron runs inside the extension host.
'use strict';
const path = require('node:path');
const Mocha = require('mocha');

exports.run = function run() {
  const mocha = new Mocha({ ui: 'bdd', timeout: 120_000, color: true });
  mocha.addFile(path.resolve(__dirname, 'smoke.test.js'));
  return new Promise((resolve, reject) => {
    mocha.run((failures) => (failures ? reject(new Error(`${failures} smoke test(s) failed`)) : resolve()));
  });
};
