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

// QuotaCalculationStep takis in the cluster queue, the result of the previous step as input and the options defined for this step in the ClusterQueue Spec.
// It returns the effective quota it calculated, a boolean indicating whether the quota update chain should continue or cancel, and an error if occured (which will also cancel the chain).
type QuotaCalculationStep = func(ctx context.Context, cq *kueue.ClusterQueue, input ResourceGroups, opts StepOptions) (result ResourceGroups, cont bool, err error)
type ResourceGroups = []kueue.ResourceGroup
type StepOptions = map[kueue.QuotaCalculationStepConfigField]string
type QuotaCalcucaltionSteps = map[kueue.QuotaCalculationStepID]QuotaCalculationStep

type QuotaManager struct {
	client                 client.Client
	quotaCaches            map[kueue.ClusterQueueReference]*QuotaCache
	quotaCalcucaltionSteps QuotaCalcucaltionSteps
}

// QuotaCache is the internal cache of the QuotaManager.
// It is used by QuotaUpdateSteps to pass partial quota calculation results to following steps.
type QuotaCache struct {
	sync.RWMutex
	// The field representing the value we want to be present in the spec of the CQ.
	cache map[kueue.QuotaCalculationStepID]ResourceGroups
}

func NewQuotaManager() *QuotaManager {
	return &QuotaManager{
		quotaCaches:            make(map[kueue.ClusterQueueReference]*QuotaCache),
		quotaCalcucaltionSteps: make(QuotaCalcucaltionSteps),
	}
}

func (qm *QuotaManager) WithStep(id kueue.QuotaCalculationStepID, step QuotaCalculationStep) {
	qm.quotaCalcucaltionSteps[id] = step
}

func (qm *QuotaManager) TriggerQuotaUpdate(ctx context.Context, caller kueue.QuotaCalculationStepID, cq *kueue.ClusterQueue) (updatePossible bool, err error) {
	if cq.Spec.QuotaAutomationConfig.Mode != kueue.Automated {
		// Nothing to do.
		return false, nil
	}
	cqRef := kueue.ClusterQueueReference(cq.Name)
	if _, ok := qm.quotaCaches[cqRef]; !ok {
		qm.quotaCaches[cqRef] = &QuotaCache{
			cache: make(map[kueue.QuotaCalculationStepID]ResourceGroups),
		}
	}
	return qm.quotaCaches[cqRef].updateQuota(ctx, qm.client, cq, caller, &qm.quotaCalcucaltionSteps)
}

func (c *QuotaCache) updateQuota(ctx context.Context, cli client.Client, cq *kueue.ClusterQueue, startStep kueue.QuotaCalculationStepID, updateFuncs *QuotaCalcucaltionSteps) (bool, error) {
	c.Lock()
	defer c.Unlock()

	stepIdx, ok := c.getStepIdx(startStep, cq)
	if !ok {
		// This step is not configured for this ClusterQueue.
		return false, nil
	}

	newCachedData := make(map[kueue.QuotaCalculationStepID]ResourceGroups)
	for _, step := range cq.Spec.QuotaAutomationConfig.AutomatedQuotaCalculationSteps[:stepIdx] {
		_, supported := (*updateFuncs)[step.ID]
		if !supported {
			// ClusterQueue has an illegal step configured.
			return false, fmt.Errorf("ClusterQueue %s: step %s not supported", cq.Name, step.ID)
		}
		newCachedData[step.ID] = c.cache[step.ID]
	}

	var lastResult ResourceGroups = ResourceGroups{}
	if stepIdx > 0 {
		prevStep := cq.Spec.QuotaAutomationConfig.AutomatedQuotaCalculationSteps[stepIdx-1]
		lastResult = c.cache[prevStep.ID]
	}

	for _, step := range cq.Spec.QuotaAutomationConfig.AutomatedQuotaCalculationSteps[stepIdx:] {
		updateFunc, supported := (*updateFuncs)[step.ID]
		if !supported {
			// ClusterQueue has an illegal step configured.
			return false, fmt.Errorf("ClusterQueue %s: step %s not supported", cq.Name, step.ID)
		}

		result, cont, err := updateFunc(ctx, cq, lastResult, cq.Spec.QuotaAutomationConfig.AutomationConfigMap)
		if err != nil {
			return true, err
		}

		newCachedData[step.ID] = result
		lastResult = result

		if !cont {
			return true, nil
		}
	}

	c.cache = newCachedData
	if !equality.Semantic.DeepEqual(cq.Spec.ResourceGroups, lastResult) {
		cq.Spec.ResourceGroups = lastResult
		return true, cli.Update(ctx, cq)
	}
	return true, nil
}

func (c *QuotaCache) getStepIdx(stepID kueue.QuotaCalculationStepID, cq *kueue.ClusterQueue) (int, bool) {
	for i, step := range cq.Spec.QuotaAutomationConfig.AutomatedQuotaCalculationSteps {
		if step.ID == stepID {
			return i, true
		}
	}
	return 0, false
}
