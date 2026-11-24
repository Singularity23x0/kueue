---
name: 📦 New Release
about: Propose a new release
title: 📦 Release v0.x.0
assignees: mimowo, tenzen-y
labels: area/release

---

## Release Checklist
<!--
Please do not remove items from the checklist
-->
- [ ] **[1]** Verify that the changelog in this issue is up-to-date by running `/sync-release-notes` (or locally `GITHUB_USER=<your-user> ./hack/releasing/sync-notes.sh $VERSION`). <!-- step:sync-release-notes --> <!-- step-meta:{"kind":"prereq","description":"Sync release notes (/sync-release-notes)"} -->
- [ ] **[2]** (Optional) For major and minor releases, run ChatOps command `/create-release-candidate` on this issue to publish a pre-release build for community testing. <!-- step:create-release-candidate --> <!-- step-meta:{"kind":"prereq","description":"Create release candidate (/create-release-candidate)","applies_to":"major_minor","optional":true} -->
- [ ] **[3]** [OWNERS](https://github.com/kubernetes-sigs/kueue/blob/main/OWNERS) must LGTM the release proposal. <!-- step-meta:{"kind":"prereq","description":"OWNERS LGTM on the release proposal","text_hints":["OWNERS","LGTM"]} -->
  At least two for minor or major releases. At least one for a patch release.
- [ ] **[4]** For major or minor releases (`v$MAJ.$MIN.0`), use the `/create-release-branch` ChatOps command to create a new release branch. <!-- step:create-release-branch --> <!-- step-meta:{"kind":"prereq","description":"Create release branch (/create-release-branch)","applies_to":"major_minor"} -->
- [ ] **[5]** Update the release branch:
  - [ ] **[5.1]** Run `GITHUB_USER=<your-user> ./hack/releasing/prepare_pull.sh --target release $VERSION` locally. <!-- step:prepare-pull-release --> <!-- step-meta:{"kind":"local_script","description":"Prepare release branch PR for {version}","command":["./hack/releasing/prepare_pull.sh","--target","release","{version}"]} -->
  - [ ] **[5.2]** Wait for this PR to merge <!-- PREPARE_PULL_RELEASE --> <!-- example #211 --> <!-- step-meta:{"kind":"wait_pr","description":"release branch preparation PR","pr_default_repo":"{kueue_repo}"} -->
- [ ] **[6]** Run ChatOps command `/tag-release` on this issue. This will: <!-- step:tag-release --> <!-- step-meta:{"kind":"chatops","description":"Tag release","command":"tag-release"} -->
  - Extract the changelog from the issue description.
  - Create the release tag at the tip of the release branch.
  - Push the tag upstream (triggers Prow to build and publish staging container image: `us-central1-docker.pkg.dev/k8s-staging-images/kueue/kueue:$VERSION`).
- [ ] **[7]** Use `/wait-for-images` to await for the staging images. <!-- step:wait-for-images --> <!-- step-meta:{"kind":"chatops","description":"Wait for staging images","command":"wait-for-images","success_markers":["✅","### Images Ready"]} -->
- [ ] **[8]** Create the draft release and promote images and Helm Charts to production (the two steps below are independent and can run in parallel):
  - [ ] **[8.1]** Run ChatOps command `/create-draft-release` on this issue. This will: <!-- step:create-draft-release --> <!-- step-meta:{"kind":"chatops","description":"Create draft release","command":"create-draft-release","required_steps":["7"]} -->
    - Extract the changelog from the issue description.
    - Create the draft release pointing out to the created tag.
    - Write the change log into the draft release.
    - Generate the artifacts in the `release-artifacts` folder.
    - Upload the files in the `release-artifacts` folder to the draft release.
    - Generate openvex data.
    - Generate the SBOM and add it to the release.
  - [ ] **[8.2]** Promote images and Helm Charts to production: <!-- step:wait-for-prod-images -->
    - [ ] **[8.2.1]** Run `GITHUB_USER=<your-user> ./hack/releasing/promote_pull.sh $VERSION` to submit the promotion PR <!-- step:promote-pull --> <!-- step-meta:{"kind":"local_script","description":"Promote staging images and Helm charts for {version}","command":["./hack/releasing/promote_pull.sh","{version}"],"required_steps":["7"]} -->
    - [ ] **[8.2.2]** Wait for the PR to be merged <!-- K8S_IO_PULL --> <!-- example kubernetes/k8s.io#7899 --> <!-- step-meta:{"kind":"wait_pr","description":"kubernetes/k8s.io promotion PR","pr_default_repo":"kubernetes/k8s.io"} -->
    - [ ] **[8.2.3]** Use `/wait-for-prod-images` to verify that the promoted images are available. <!-- step:wait-for-prod-images --> <!-- step-meta:{"kind":"chatops","description":"Wait for production images","command":"wait-for-prod-images","success_markers":["✅","### Images Ready"]} -->
- [ ] **[9]** Use `/publish-release` to publish the release prepared at the [GitHub releases page](https://github.com/kubernetes-sigs/kueue/releases). <!-- step:publish-release --> <!-- step-meta:{"kind":"chatops","description":"Publish release","command":"publish-release","required_steps":["8.1","8.2.3"]} -->
      Link: <!-- RELEASE_LINK --> <!-- example https://github.com/kubernetes-sigs/kueue/releases/tag/v0.1.0 -->
- [ ] **[10]** For the latest published release, run ChatOps command `/update-krew-index` on this issue to submit the krew plugin release. <!-- step:update-krew-index --> <!-- step-meta:{"kind":"chatops","description":"Update krew index","command":"update-krew-index","only_if_latest":true} -->
- [ ] **[11]** Update the `main` branch :
  - [ ] **[11.1]** Run `GITHUB_USER=<your-user> ./hack/releasing/prepare_pull.sh --target main $VERSION` locally. <!-- step:prepare-pull-main --> <!-- step-meta:{"kind":"local_script","description":"Prepare main branch PR for {version}","command":["./hack/releasing/prepare_pull.sh","--target","main","{version}"],"required_steps":["9"]} -->
    - *Note: The script automatically detects if a newer version is already out and skips version updates if so. Specifying `--skip-version-updates` is not necessary in a default workflow.*
    - *Note: Versioned docs are handled automatically during releases -- major, minor, and patch: the
    `main`-update PR from `prepare_pull.sh` runs `hack/releasing/snapshot-docs.py`, which
    freezes the release's docs into `site/content/<locale>/v$MAJ.$MIN/docs`, adds it to the
    version dropdown, and prunes old snapshots. Patch releases re-freeze the existing snapshot
    to the new patch version (e.g. v0.17.7 -> v0.17.8). No Netlify/DNS steps are required. Just
    confirm the snapshot dirs and the `[[params.versions]]` entry are present in that PR.*
  - [ ] **[11.2]** Wait for this PR to merge <!-- PREPARE_PULL_MAIN --> <!-- example #214 --> <!-- step-meta:{"kind":"wait_pr","description":"main branch update PR","pr_default_repo":"{kueue_repo}"} -->
  - [ ] **[11.3]** For patch release, cherry-pick the pull request onto the `website` branch. <!-- step-meta:{"kind":"manual","description":"Cherry-pick main PR onto website branch","text_hints":["cherry-pick","website"],"applies_to":"patch","confirm_message":"Cherry-pick the merged main-update PR onto the `website` branch."} -->
- [ ] **[12]** For major and minor releases, use `/merge-main-into-website` to merge the `main` branch into the `website` branch to publish the updated documentation. <!-- step:merge-main-into-website --> <!-- step-meta:{"kind":"chatops","description":"Merge main into website","command":"merge-main-into-website","applies_to":"major_minor"} -->
- [ ] **[13]** Send an announcement email to `sig-scheduling@kubernetes.io` and `wg-batch@kubernetes.io` with the subject `[ANNOUNCE] kueue $VERSION is released`. <!-- step-meta:{"kind":"manual","description":"Send announcement email","text_hints":["announcement email"],"confirm_message":"Send announcement email to sig-scheduling@kubernetes.io and wg-batch@kubernetes.io with subject: [ANNOUNCE] kueue {version} is released"} -->   <!--Link: example https://groups.google.com/a/kubernetes.io/g/wg-batch/c/-gZOrSnwDV4 -->
- [ ] **[14]** For a major or minor release, prepare the repo for the next version:
  - [ ] **[14.1]** Run the ChatOps command `/create-devel-tag` on this issue. This will: <!-- step:create-devel-tag --> <!-- step-meta:{"kind":"chatops","description":"Create devel tag","command":"create-devel-tag","applies_to":"major_minor"} -->
        create and push an unannotated `_devel` tag
        (`v$MAJ.$(($MIN+1)).0-devel`) on `main`, so that development builds
        from the `main` branch have a meaningful version number.
  - [ ] **[14.2]** Create a milestone for the next minor release and update prow to set it automatically for new PRs:
    - [ ] **[14.2.1]** Run the ChatOps command `/create-milestone` on this issue to create the `v$MAJ.$(($MIN+1))` milestone. <!-- step:create-milestone --> <!-- step-meta:{"kind":"chatops","description":"Create next minor milestone","command":"create-milestone","applies_to":"major_minor"} -->
    - [ ] **[14.2.2]** Run `GITHUB_USER=<your-user> ./hack/releasing/milestone_pull.sh $VERSION` to submit the `milestone_applier` PR. <!-- step:milestone-pull --> <!-- step-meta:{"kind":"local_script","description":"Submit milestone_applier PR for {version}","command":["./hack/releasing/milestone_pull.sh","{version}"],"applies_to":"major_minor"} -->
    - [ ] **[14.2.3]** Wait for this PR to merge <!-- MILESTONE_PULL --> <!-- example https://github.com/kubernetes/test-infra/pull/30222 --> <!-- step-meta:{"kind":"wait_pr","description":"kubernetes/test-infra milestone PR","pr_default_repo":"kubernetes/test-infra","applies_to":"major_minor"} -->
  - [ ] **[14.3]** Create the presubmits and periodic jobs for the next patch release, and remove the CI jobs for testing the unsupported branch:
    - [ ] **[14.3.1]** Run `GITHUB_USER=<your-user> ./hack/releasing/ci_pull.sh $VERSION` locally. <!-- step:ci-pull --> <!-- step-meta:{"kind":"local_script","description":"Submit CI jobs PR for {version}","command":["./hack/releasing/ci_pull.sh","{version}"],"applies_to":"major_minor"} -->
    - [ ] **[14.3.2]** Wait for this PR to merge <!-- CI_PULL --> <!-- example https://github.com/kubernetes/test-infra/pull/37948 --> <!-- step-meta:{"kind":"wait_pr","description":"kubernetes/test-infra CI PR","pr_default_repo":"kubernetes/test-infra","applies_to":"major_minor"} -->
- [ ] **[15]** Run the ChatOps command `/unpin-release-issue` on this issue, then close it. <!-- step:unpin-release-issue --> <!-- step-meta:{"kind":"chatops","description":"Unpin and close release issue","command":"unpin-release-issue","close_issue_after":true,"confirm_message":"Close release issue #{issue_number} ({issue_url})?"} -->


## Changelog

<!-- release-changelog-start -->
```markdown
Describe changes since the last release here.
```
<!-- release-changelog-end -->
