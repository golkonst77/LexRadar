define('custom:lexradar/opportunity-modal', ['views/modals/edit'], function (EditModal) {
    return class extends EditModal {
        async setupLate() {
            await super.setupLate();
            // Standard form validation/save; only this model uses the idempotent endpoint.
            this.model.url = `LexRadar/Task/${encodeURIComponent(this.options.taskId)}/opportunity`;
            this.model.set({amount: null, closeDate: null});
        }

        createRecordView(model, callback) {
            super.createRecordView(model, record => {
                ['account', 'assignedUser', 'stage'].forEach(field => record.setFieldReadOnly(field));
                if (callback) callback(record);
            });
        }
    };
});
