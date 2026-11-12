define('custom:lexradar/task-opportunity', ['action-handler'], function (ActionHandler) {
    return class extends ActionHandler {
        isVisible() {
            const model = this.view.model;
            const name = model.get('name') || '';
            return (name === 'Контрольный звонок LexRadar' ||
                name.startsWith('Контрольный звонок LexRadar · ')) &&
                this.view.getAcl().checkModel(model, 'read') &&
                this.view.getAcl().checkScope('Opportunity', 'create');
        }

        async run() {
            if (this.busy) return;
            this.busy = true;
            try {
                const taskId = this.view.model.id;
                const result = await Espo.Ajax.getRequest(`LexRadar/Task/${encodeURIComponent(taskId)}/opportunity`);
                if (result.id) {
                    this.view.getRouter().navigate(`#Opportunity/view/${result.id}`, {trigger: true});
                    return;
                }
                const modal = await this.view.createView('lrOpportunityModal', 'custom:lexradar/opportunity-modal', {
                    entityType: 'Opportunity',
                    taskId: taskId,
                    attributes: {...result.attributes, amount: null, closeDate: null},
                    layoutName: 'detail',
                    fullFormDisabled: true,
                });
                this.view.listenToOnce(modal, 'after:save', model => {
                    this.view.getRouter().navigate(`#Opportunity/view/${model.id}`, {trigger: true});
                });
                await modal.render();
            } finally {
                this.busy = false;
            }
        }
    };
});
