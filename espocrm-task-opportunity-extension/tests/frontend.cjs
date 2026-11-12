const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let Handler, Modal;
class ActionHandler { constructor(view) { this.view = view; } }
class EditModal { async setupLate() {} }
const context = {define: (name, deps, factory) => {
    if (name.endsWith('task-opportunity')) Handler = factory(ActionHandler);
    else Modal = factory(EditModal);
}, Espo: {Ajax: {getRequest: async () => ({id: 'existing'})}}};
for (const f of ['task-opportunity.js', 'opportunity-modal.js']) {
    vm.runInNewContext(fs.readFileSync(`files/client/custom/src/lexradar/${f}`, 'utf8'), context);
}
(async () => {
    let navigated;
    const handler = new Handler({model: {id:'task1', get: () => 'Контрольный звонок LexRadar'},
        getAcl: () => ({checkModel: () => true, checkScope: () => true}),
        getRouter: () => ({navigate: url => navigated = url})});
    assert.equal(handler.isVisible(), true);
    await handler.run(); assert.equal(navigated, '#Opportunity/view/existing');
    assert.equal(handler.busy, false);
    let rendered = false;
    let options;
    context.Espo.Ajax.getRequest = async url => {
        assert.equal(url, 'LexRadar/Task/task1/opportunity');
        return {attributes: {accountId: 'account1', accountName: 'Clinic'}};
    };
    handler.view.createView = async (name, type, opts) => {
        options = opts;
        return {render: async () => {rendered = true;}};
    };
    handler.view.listenToOnce = () => {};
    await handler.run();
    assert.equal(rendered, true);
    assert.equal(options.attributes.accountId, 'account1');
    assert.equal(options.attributes.amount, null);
    assert.equal(options.attributes.closeDate, null);
    const modal = new Modal();
    modal.options = {taskId:'task1'};
    let values;
    modal.model = {set: v => values = v};
    await modal.setupLate();
    assert.equal(modal.model.url, 'LexRadar/Task/task1/opportunity');
    assert.equal(values.amount, null); assert.equal(values.closeDate, null);
    console.log('Frontend: button, existing redirect, protected URL and empty user values: OK');
})().catch(e => {console.error(e); process.exit(1);});
