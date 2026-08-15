{{/*
Expand the name of the chart.
*/}}
{{- define "floodgate.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name. Truncated to 63 chars for label compatibility.
*/}}
{{- define "floodgate.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name and version, used as a label.
*/}}
{{- define "floodgate.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every object the chart owns.
*/}}
{{- define "floodgate.labels" -}}
helm.sh/chart: {{ include "floodgate.chart" . }}
{{ include "floodgate.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels — used for matchLabels in the Deployment and the Service
selector. Must be stable across upgrades.
*/}}
{{- define "floodgate.selectorLabels" -}}
app.kubernetes.io/name: {{ include "floodgate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Resolve the ServiceAccount name to use.
*/}}
{{- define "floodgate.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "floodgate.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Resolve the container image reference. Falls back to .Chart.appVersion when
.Values.image.tag is empty.
*/}}
{{- define "floodgate.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end }}
