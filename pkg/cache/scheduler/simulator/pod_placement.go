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

package simulator

type podPlacement struct {
	Node           string
	Error          error
	FailureReasons []string
}

// NewSuccessfulPlacement returns a PodPlacement that
// represents a bainding of a Pod to a Node.
func NewSuccessfulPlacement(node string) *podPlacement {
	return &podPlacement{
		Node: node,
	}
}

// NewFailedPlacement returns a PodPlacement representing
// a Pod that failed to be scheduled.
func NewFailedPlacement(reasons ...string) *podPlacement {
	return &podPlacement{
		FailureReasons: reasons,
	}
}

// NewPlacementError returns a PodPlacement representing
// a Pod that failed to be scheduled due to an explicit error.
func NewPlacementError(err error, reasons ...string) *podPlacement {
	return &podPlacement{
		Error:          err,
		FailureReasons: reasons,
	}
}

func (p *podPlacement) IsError() bool {
	return p.Error != nil
}

func (p *podPlacement) IsSuccess() bool {
	return p.Error == nil && len(p.FailureReasons) == 0 && p.Node != ""
}
