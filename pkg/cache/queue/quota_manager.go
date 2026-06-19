/*
Copyright The Kubernetes Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package queue

import (
	"context"
	"fmt"
	"sync"

	"k8s.io/apimachinery/pkg/api/equality"
	"sigs.k8s.io/controller-runtime/pkg/client"
	kueue "sigs.k8s.io/kueue/apis/kueue/v1beta2"
)

// QuotaAutomationStepFunc takis in the cluster queue, the result of the previous step as input and the options defined for this step in the ClusterQueue Spec.
// It returns the effective quota it calculated, a boolean indicating whether the quota update chain should continue or cancel, and an error if occured (which will also cancel the chain).
type QuotaAutomationStepFunc = func(ctx context.Context, cq *kueue.ClusterQueue, input ResourceGroups) (result ResourceGroups, cont bool, err error)
type ResourceGroups = []kueue.ResourceGroup
type AutomationStepFuncs = map[kueue.QuotaAutomationStep]QuotaAutomationStepFunc

type QuotaManager struct {
	client              client.Client
	quotaCaches         map[kueue.ClusterQueueReference]*QuotaCache
	automationStepFuncs AutomationStepFuncs
}

// QuotaCache is the internal cache of the QuotaManager.
// It is used by QuotaUpdateSteps to pass partial quota calculation results to following steps.
type QuotaCache struct {
	sync.RWMutex
	data map[kueue.QuotaAutomationStep]ResourceGroups
}

func NewQuotaManager() *QuotaManager {
	return &QuotaManager{
		quotaCaches:         make(map[kueue.ClusterQueueReference]*QuotaCache),
		automationStepFuncs: make(AutomationStepFuncs),
	}
}

func (qm *QuotaManager) SetStepFunc(step kueue.QuotaAutomationStep, fn QuotaAutomationStepFunc) {
	qm.automationStepFuncs[step] = fn
}

func (qm *QuotaManager) TriggerUpdate(ctx context.Context, startStep kueue.QuotaAutomationStep, cq *kueue.ClusterQueue) (automationPossible bool, err error) {
	if cq.Spec.QuotaAutomationConfig.Mode != kueue.Automated {
		return false, nil
	}
	cqRef := kueue.ClusterQueueReference(cq.Name)
	if _, ok := qm.quotaCaches[cqRef]; !ok {
		qm.quotaCaches[cqRef] = &QuotaCache{
			data: make(map[kueue.QuotaAutomationStep]ResourceGroups),
		}
	}
	return qm.quotaCaches[cqRef].updateQuota(ctx, qm.client, cq, startStep, &qm.automationStepFuncs)
}

func (qc *QuotaCache) updateQuota(ctx context.Context, client client.Client, cq *kueue.ClusterQueue, startStep kueue.QuotaAutomationStep, stepFuncs *AutomationStepFuncs) (bool, error) {
	qc.Lock()
	defer qc.Unlock()

	stepIdx, ok := qc.getStepIdx(startStep, cq)
	if !ok {
		// This step is not configured for this ClusterQueue.
		return false, nil
	}

	newCachedData := make(map[kueue.QuotaAutomationStep]ResourceGroups)

	// 1. Copy the data of previous steps.
	for _, step := range cq.Spec.QuotaAutomationConfig.Steps[:stepIdx] {
		_, supported := (*stepFuncs)[step]
		if !supported {
			// ClusterQueue has an unsupported step configured.
			return false, fmt.Errorf("ClusterQueue %s: step %s not supported", cq.Name, step)
		}
		newCachedData[step] = qc.data[step]
	}

	lastResult := ResourceGroups{}
	if stepIdx > 0 {
		prevStep := cq.Spec.QuotaAutomationConfig.Steps[stepIdx-1]
		lastResult = qc.data[prevStep]
	}

	// 2. Perform all steps starting from startStep.
	for _, step := range cq.Spec.QuotaAutomationConfig.Steps[stepIdx:] {
		updateFunc, supported := (*stepFuncs)[step]
		if !supported {
			// ClusterQueue has an unsupported step configured.
			return false, fmt.Errorf("ClusterQueue %s: step %s not supported", cq.Name, step)
		}

		result, cont, err := updateFunc(ctx, cq, lastResult)
		if !cont || err != nil {
			return true, err
		}

		newCachedData[step] = result
		lastResult = result
	}

	qc.data = newCachedData
	if !equality.Semantic.DeepEqual(cq.Spec.ResourceGroups, lastResult) {
		cq.Spec.ResourceGroups = lastResult
		return true, client.Update(ctx, cq)
	}
	return true, nil
}

func (c *QuotaCache) getStepIdx(soughtStep kueue.QuotaAutomationStep, cq *kueue.ClusterQueue) (int, bool) {
	for idx, step := range cq.Spec.QuotaAutomationConfig.Steps {
		if step == soughtStep {
			return idx, true
		}
	}
	return 0, false
}
