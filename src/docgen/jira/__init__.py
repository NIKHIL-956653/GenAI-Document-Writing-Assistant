"""Jira Cloud integration: fetch an issue and normalise it into a Ticket."""
from docgen.jira.adf import adf_to_text
from docgen.jira.client import JiraClient

__all__ = ["JiraClient", "adf_to_text"]
